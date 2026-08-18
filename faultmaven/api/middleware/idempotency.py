"""Idempotency Middleware

Purpose: Handle Idempotency-Key headers for POST operations with Redis persistence

This middleware implements idempotency semantics for POST requests by:
- Storing request/response pairs in Redis with TTL
- Returning cached responses for duplicate idempotency keys
- Ensuring atomic operations across server restarts
- Supporting proper error handling and replay scenarios

Architecture Integration:
- Uses container.py dependency injection for Redis client
- Integrates with logging system for correlation tracking
- Follows FastAPI middleware patterns
"""

import hashlib
import json
import logging
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# Path fragments that must never participate in idempotency replay. Replaying a
# token mint is not idempotency: it serves one caller's credential to whoever
# presents the key next. Excluded structurally rather than relying on the cache
# key being scoped correctly. Erring broad is deliberate.
EXCLUDED_PATH_MARKERS = ("/auth/",)

# Exact paths excluded from idempotency. Kept separate from the substring
# markers above because the obvious substring is unusable here: ``POST
# /api/v1/sessions`` mints a session id already bound to a user_id
# (``modules/auth/api/session.py``), and a bare session id is still accepted as
# proof of that identity wherever no ``Authorization`` header is present —
# ``create_case_for_session`` in ``modules/case/api/routes.py`` derives its
# ``user_id`` from ``session.user_id`` for exactly those callers. That makes the
# mint response a credential: replaying it serves one caller's identity to
# whoever presents the key next.
#
# The invariant to re-check before dropping this entry is "no route treats a
# session id alone as identity" — not the continued existence of any single
# call site. Call sites move; the exclusion stops being necessary only when
# nothing derives identity from a session id.
#
# A ``/sessions`` substring marker is not an option: it catches fifteen POST
# routes in this app, including ``/api/v1/cases/sessions/{session_id}/case``,
# silently disabling idempotency on exactly the route that needs it. Match the
# minting route and nothing else.
EXCLUDED_EXACT_PATHS = frozenset({"/api/v1/sessions"})

# Upper bound on a request body we are willing to buffer for fingerprinting.
# Anything larger is left unbuffered (and unfingerprinted) rather than held in
# memory on every request.
MAX_FINGERPRINTED_BODY_BYTES = 256 * 1024

# Sentinel used in the cache key when the body was deliberately not buffered.
# Not a hex digest, so it can never collide with a real fingerprint.
UNFINGERPRINTED_BODY = "body-unfingerprinted"


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Middleware to handle idempotency keys for POST operations."""

    def __init__(self, app: ASGIApp, redis_client=None):
        super().__init__(app)
        self.redis_client = redis_client
        self.ttl_seconds = 3600  # 1 hour TTL for idempotency keys
        self.key_prefix = "idempotency:"
        self._resolved = redis_client is not None

    def _ensure_redis(self, request: Request) -> None:
        """Resolve the Redis client lazily on first use.

        Starlette middleware is constructed at import time, before the lifespan
        startup that creates the Redis client — so a client passed in __init__ is
        None at that point. ``resolve_redis_client`` performs the shared lazy
        resolution (injected → app.state → central factory) and always returns a
        working client.
        """
        if self._resolved:
            return
        from ...infrastructure.redis_client import resolve_redis_client

        self.redis_client = resolve_redis_client(request, injected=self.redis_client)
        self._resolved = True

    async def dispatch(self, request: Request, call_next):
        """Process request with idempotency checking."""

        # Only handle POST requests
        if request.method != "POST":
            return await call_next(request)

        # Authentication endpoints never participate: a replayed token mint
        # hands the first caller's credential to the next one.
        if self._is_excluded_path(request.url.path):
            return await call_next(request)

        # Check for idempotency key
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return await call_next(request)

        # Validate idempotency key format (UUID-like)
        if not self._is_valid_idempotency_key(idempotency_key):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "Invalid Idempotency-Key format. Must be a valid UUID or similar identifier.",
                    "error_type": "InvalidIdempotencyKey",
                    "correlation_id": str(uuid4()),
                    "timestamp": self._get_timestamp(),
                },
            )

        # Scope the cache to the principal behind the request. A request that
        # carries no credential must not participate in idempotency in either
        # direction — it may neither read nor write the cache. Unidentified
        # callers would otherwise all share one bucket, and because the cache
        # lookup happens before ``call_next`` (route-level ``Depends`` auth
        # never runs on a hit) an unauthenticated request could be served an
        # authenticated caller's response body.
        caller_identity = await self._caller_identity(request)
        if caller_identity is None:
            return await call_next(request)

        # Whether the downstream stack has already been invoked. The recovery
        # path below may only retry work that never reached the route: once
        # ``call_next`` has been entered the route may have run (or partially
        # run), and calling it again would execute the handler a second time
        # for a single client request.
        downstream_invoked = False

        try:
            # Resolve the Redis client lazily (after startup has populated
            # app.state). Kept inside the try so any failure during resolution
            # degrades gracefully (process the request) rather than 500-ing.
            self._ensure_redis(request)
            if self.redis_client is None:
                downstream_invoked = True
                return await call_next(request)

            # Create cache key
            body_fingerprint = await self._body_fingerprint(request)
            cache_key = self._create_cache_key(
                idempotency_key, request, caller_identity
            )

            # Check for existing response
            cached_response = await self._get_cached_response(cache_key)
            if cached_response:
                # The body is compared here rather than folded into the key.
                # Keying on it would make a reused key with a different body a
                # cache *miss*, which silently executes a second time and
                # creates a second resource. Replay only when the stored and
                # incoming bodies are shown to be the same:
                #
                #   both fingerprinted and equal -> replay
                #   both unfingerprinted         -> replay (equal sentinels)
                #   different, or only one       -> 409, do not execute
                #
                # The both-unfingerprinted case is load-bearing, not a
                # loophole: the copilot's hot retry path (multipart turn
                # submission) is never fingerprinted, so refusing it would
                # break this feature's main consumer.
                if cached_response.get("body_fingerprint") != body_fingerprint:
                    logger.warning(
                        f"Idempotency key reused with a different request body: "
                        f"{idempotency_key}"
                    )
                    return JSONResponse(
                        status_code=409,
                        # Labelled, and that is load-bearing rather than
                        # decorative. Several unrelated conflicts share 409, and
                        # a client cannot tell them apart from the body: the
                        # Slack agent reads an **unlabelled** 409 on the turn
                        # POST as "this case is terminal" and tells the user
                        # their investigation is closed. Key reuse is not
                        # terminal and not even about the case, so going out
                        # unlabelled would make it a false claim about the
                        # user's investigation. A label the client does not
                        # recognize falls through to its generic 4xx handling,
                        # which is the honest answer for a conflict it does not
                        # model — so labelling can only narrow the damage.
                        headers={"x-error-code": "IDEMPOTENCY_KEY_REUSE"},
                        content={
                            "detail": (
                                "This Idempotency-Key was already used with a "
                                "different request body. Use a new key for a "
                                "different request, or retry the original "
                                "request unchanged."
                            ),
                            "error_type": "IdempotencyKeyReuse",
                            "error_code": "IDEMPOTENCY_KEY_REUSE",
                            "correlation_id": str(uuid4()),
                            "timestamp": self._get_timestamp(),
                        },
                    )

                logger.info(
                    f"Returning cached response for idempotency key: {idempotency_key}"
                )
                return self._create_response_from_cache(cached_response)

            # Process request normally
            downstream_invoked = True
            response = await call_next(request)

            # Cache successful responses (2xx status codes)
            if 200 <= response.status_code < 300:
                await self._cache_response(
                    cache_key, response, idempotency_key, body_fingerprint
                )

            return response

        except Exception as e:
            if downstream_invoked:
                # The route already ran, or failed on its way to running.
                # Retrying here would execute a non-idempotent handler twice
                # for one client request — exactly what this middleware exists
                # to prevent. Let the error surface to the error handlers.
                raise
            logger.error(f"Error in idempotency middleware: {e}")
            # The failure happened before the request reached the route, so
            # degrading to a normal, uncached request is safe.
            return await call_next(request)

    def _is_valid_idempotency_key(self, key: str) -> bool:
        """Validate idempotency key format."""
        if not key or len(key) < 8 or len(key) > 255:
            return False

        # Allow UUID-like strings, alphanumeric with hyphens/underscores
        import re

        pattern = r"^[a-zA-Z0-9_-]+$"
        return bool(re.match(pattern, key))

    def _is_excluded_path(self, path: str) -> bool:
        """Whether this path is structurally excluded from idempotency.

        The trailing slash is normalised away first: this middleware is
        installed *outside* ``TrailingSlashMiddleware``, so it sees the path as
        the client sent it. Without normalising, ``POST /api/v1/sessions/``
        would slip past an exact-path exclusion that ``POST /api/v1/sessions``
        is caught by.
        """
        normalized = path.rstrip("/") or "/"
        if normalized in EXCLUDED_EXACT_PATHS:
            return True
        return any(marker in path for marker in EXCLUDED_PATH_MARKERS)

    def _auth_service(self, request: Request):
        """The verifier used to name the principal, or ``None``.

        Read straight off ``app.state`` — wired by the composition root in
        ``main.py`` beside the other auth services — rather than through
        ``api.middleware.auth.get_auth_service``, whose fallback *constructs* an
        ``AuthService`` when none is wired. Constructing one per request inside
        a middleware would load keys (and generate a development RSA pair) on
        the hot path. An app without the service wired simply falls back to the
        raw-credential scope below, which is what this middleware did for every
        request before fm#1087.
        """
        try:
            return getattr(request.app.state, "auth_service", None)
        except Exception:  # pragma: no cover - defensive
            return None

    async def _caller_identity(self, request: Request) -> Optional[str]:
        """Name the principal this request belongs to, as a cache scope.

        ``Authorization`` is **required**: it is the only header a caller cannot
        simply choose. ``X-Session-ID`` is client-supplied and guessable, so
        accepting it alone would let one caller pre-seed a bucket that a later
        caller with the same session id is then served from.

        Returns ``None`` when there is no ``Authorization`` header, which the
        dispatcher treats as "do not participate in idempotency" — the same
        fail-closed shape as ``DeduplicationMiddleware`` returning a ``None``
        request hash when there is no session id.

        **Scope the bucket to the principal, not to the credential (fm#1087).**
        Hashing the raw ``Authorization`` header put every rotation of a
        caller's credential in a different bucket. The copilot refreshes its
        access token periodically, so a retry issued after a refresh carried a
        new bearer string, missed the cache and executed the turn a second time
        — committing a duplicate message to the case. That is the identical
        failure this middleware already reasons about for ``X-Session-ID``,
        which is excluded *because* it rotates; the access token rotates the
        same way, on the same 401 -> refresh -> retry path, and the exclusion
        had only ever been applied to one of the two rotating identifiers.

        One authenticated principal replaying its own response across two
        access tokens is correct behaviour, not a leak — exactly as it is
        across two session ids.

        **Why ``sub`` is safe here and was not before.** The previous docstring
        refused to key on a JWT claim, and its reasoning was right for the code
        as written: this middleware runs before route-level ``Depends`` auth, so
        an *undecoded* claim is attacker-controlled, and because a cache hit
        returns before ``call_next`` a forged token would have been a
        bucket-selection primitive over other callers' response bodies. That is
        a statement about ordering, not an immutable constraint. The token is
        now verified **here**, by the same
        ``AuthService.verify_token_with_revocation_check`` the mandatory-auth
        middleware, the tenant binder and the optional-auth dependency use:
        signature, expiry, issuer, audience, required claims, ``type ==
        "access"`` and the revocation list. A ``sub`` that survives that is not
        forgeable without already holding a live credential for that principal.

        Verification failure is **not** an error path: an unverifiable
        ``Authorization`` header names no principal, so the identity falls back
        to the raw credential hash — the narrowest scope available, reachable
        only by a caller presenting that byte-identical header. Narrowing can
        cost a replay; it can never serve one caller another's body. This is
        also the path an app takes when no ``AuthService`` is wired, and it is
        what keeps a non-JWT bearer scheme scoped rather than silently
        unprotected.

        The two scopes are namespaced apart so a verified principal and a raw
        credential can never hash into the same bucket. Both are hashed, so no
        credential material and no user id reaches Redis keys or logs.

        The namespacing does mean one *new* fork, and it is deliberate: a token
        that verified on the first attempt and has since expired or been revoked
        moves from the ``sub`` scope to the ``raw`` one, so that retry misses
        where the old code would have replayed. On every surface this feature
        exists for — turn submission, case creation, closure, reports — the miss
        cannot duplicate anything: they take ``require_authentication``, which
        rejects that same dead token, so the request falls through to the 401 it
        was going to get anyway, and a 401 is never cached.

        The exhaustive residual, stated rather than waved at: three POST routes
        admit an unverified caller via ``get_current_user_optional``. Two are
        searches (``/knowledge/search``, ``/knowledge/documents/search``), which
        re-run harmlessly. The third, ``/cases/sessions/{sid}/case``, falls back
        to the session's user — and only its ``force_new=true`` variant creates
        anything, since ``force_new=false`` returns the session's existing case
        by construction. A dead-credential retry there re-runs where it used to
        replay. ``POST /api/v1/sessions`` is already excluded structurally.

        The copilot reaches none of that: on a 401 it refreshes and retries with
        a *new* token, which is the case this change exists to fix.
        """
        authorization = request.headers.get("Authorization")
        if not authorization:
            return None

        principal = await self._verified_principal(request, authorization)
        if principal is not None:
            return f"sub:{hashlib.sha256(principal.encode('utf-8')).hexdigest()}"

        return f"raw:{hashlib.sha256(authorization.encode('utf-8')).hexdigest()}"

    async def _verified_principal(
        self, request: Request, authorization: str
    ) -> Optional[str]:
        """The verified ``sub`` of the bearer token, or ``None``.

        Every failure — no bearer token, no verifier wired, bad signature,
        expired, revoked, wrong token type — returns ``None`` so the caller
        falls back to the raw-credential scope. Nothing here may raise: this
        runs before ``dispatch``'s try block, and idempotency scoping must never
        turn a serviceable request into a 500.

        The lazy import is inside the guard for that reason and not by
        oversight. It is resolved at request time, so it can fail at request
        time — a circular import through a partially initialized module, or
        ``_extract_token`` being renamed in ``auth.py``, which neither startup
        nor ``lint-imports`` would catch. Outside the guard that lands as a 500
        on every POST carrying an ``Idempotency-Key``; inside it, it degrades to
        the raw scope like any other reason the principal cannot be named.
        """
        try:
            from .auth import _extract_token

            token = _extract_token(authorization, None)
            if not token:
                return None

            auth_service = self._auth_service(request)
            if auth_service is None:
                return None

            claims = await auth_service.verify_token_with_revocation_check(
                token, token_type="access"
            )
        except Exception as e:
            logger.debug(f"Idempotency scope falling back to raw credential: {e}")
            return None

        subject = claims.get("sub")
        return subject if isinstance(subject, str) and subject else None

    async def _body_fingerprint(self, request: Request) -> str:
        """Fingerprint the request body so a reused key cannot swap payloads.

        Buffering a body inside ``BaseHTTPMiddleware`` is safe here because
        Starlette wraps the request in ``_CachedRequest``: once ``body()`` has
        been awaited, ``wrapped_receive`` replays the cached bytes to the
        downstream app rather than starving it. That replay is the whole of the
        guarantee, and it is pinned by tests that assert a downstream route
        receives the exact bytes (see the body-integrity tests in
        ``tests/unit/api/middleware/test_idempotency_caller_scoping.py``) — not
        by anything this function does.

        The ``wrapped_receive`` probe below is a cheap tripwire, not the
        safety mechanism: ``BaseHTTPMiddleware.__call__`` already reads that
        attribute before dispatch, so in practice it is always present. It is
        kept only so that a future refactor placing this middleware outside
        ``BaseHTTPMiddleware`` degrades to no fingerprint instead of consuming
        a body nothing will replay.

        Buffering is skipped for anything that is not a declared, bounded JSON
        payload; upload paths (multipart, streamed, oversized) are never read.
        """
        if not hasattr(request, "wrapped_receive"):
            return UNFINGERPRINTED_BODY

        content_type = request.headers.get("content-type", "")
        if content_type.split(";")[0].strip().lower() != "application/json":
            return UNFINGERPRINTED_BODY

        content_length = request.headers.get("content-length")
        if content_length is None:
            return UNFINGERPRINTED_BODY
        try:
            declared_length = int(content_length)
        except ValueError:
            return UNFINGERPRINTED_BODY
        if declared_length > MAX_FINGERPRINTED_BODY_BYTES:
            return UNFINGERPRINTED_BODY

        try:
            body = await request.body()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"Could not buffer request body for fingerprinting: {e}")
            return UNFINGERPRINTED_BODY

        return hashlib.sha256(body).hexdigest()

    def _create_cache_key(
        self,
        idempotency_key: str,
        request: Request,
        caller_identity: str,
    ) -> str:
        """Create a Redis cache key scoped to caller and route.

        The key must not be reachable by anyone but the principal that created
        it, so caller identity is part of the hashed material rather than an
        advisory extra. It identifies the *principal* rather than the
        credential, so a retry that straddles a token refresh still lands in the
        bucket its first attempt wrote (fm#1087) — see ``_caller_identity``.

        The query string is included because it selects behaviour, not just
        presentation: ``POST /api/v1/cases/sessions/{sid}/case?force_new=true``
        and ``?force_new=false`` are different operations, and without the query
        they hash identically for one caller and key — so the second would be
        served the first's response and never run.

        The request body is deliberately **absent** here. It lives in the cached
        payload instead, so that a reused key with a different body is a
        detectable conflict rather than a cache miss that silently executes a
        second time. See ``dispatch``.
        """
        # Normalise the trailing slash, as ``_is_excluded_path`` does and for the
        # same reason: this middleware sits *outside* ``TrailingSlashMiddleware``,
        # so it sees the client's raw path while the route sees the normalised
        # one. ``/api/v1/cases`` and ``/api/v1/cases/`` reach the same handler,
        # so keying them apart would let a retry that varies only by the slash
        # execute twice. Merging them is not a widening — it makes the key agree
        # with the routing that actually happens.
        #
        # The query string keeps its exact value: unlike the trailing slash it
        # is never normalised downstream, and two different queries really are
        # two different operations.
        normalized_path = request.url.path.rstrip("/") or "/"
        method_path = f"{request.method}:{normalized_path}"
        combined = "|".join(
            [idempotency_key, method_path, request.url.query, caller_identity]
        )
        hash_suffix = hashlib.sha256(combined.encode()).hexdigest()[:32]
        return f"{self.key_prefix}{idempotency_key}:{hash_suffix}"

    async def _get_cached_response(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Retrieve cached response from Redis."""
        try:
            cached_data = await self.redis_client.get(cache_key)
            if cached_data:
                return json.loads(cached_data)
        except Exception as e:
            logger.error(f"Error retrieving cached response: {e}")
        return None

    async def _cache_response(
        self,
        cache_key: str,
        response: Response,
        idempotency_key: str,
        body_fingerprint: str,
    ):
        """Cache response in Redis with TTL.

        Caching drains ``response.body_iterator``, which is single-use, so the
        drained bytes must be put back on *every* path out of this method. If
        the restore is skipped — a Redis error between the drain and the
        restore used to do exactly that — the client receives the original
        status and Content-Length with a zero-byte body: a silent truncation of
        every idempotent POST response for as long as Redis is unreachable.
        """
        body = b""
        try:
            # Read response body
            async for chunk in response.body_iterator:
                body += chunk

            # Prepare cache data
            cache_data = {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": body.decode("utf-8") if body else "",
                "content_type": response.headers.get(
                    "content-type", "application/json"
                ),
                "idempotency_key": idempotency_key,
                # Stored so a later request under the same key can be shown to
                # carry the same body before its response is replayed.
                "body_fingerprint": body_fingerprint,
                "cached_at": self._get_timestamp(),
            }

            # Store in Redis with TTL
            await self.redis_client.setex(
                cache_key, self.ttl_seconds, json.dumps(cache_data)
            )

            logger.info(f"Cached response for idempotency key: {idempotency_key}")

        except Exception as e:
            logger.error(f"Error caching response: {e}")
        finally:
            # Always hand the drained body back to the client, whether or not
            # it was successfully cached. Caching is best-effort; delivering
            # the response is not.
            response.body_iterator = self._create_body_iterator(body)

    def _create_response_from_cache(self, cached_data: Dict[str, Any]) -> Response:
        """Create FastAPI response from cached data."""
        headers = cached_data.get("headers", {})

        # Add cache indicator header
        headers["X-Idempotency-Replayed"] = "true"

        # Create appropriate response type
        content_type = cached_data.get("content_type", "application/json")
        body = cached_data.get("body", "")

        if content_type.startswith("application/json"):
            try:
                json_body = json.loads(body) if body else {}
                return JSONResponse(
                    status_code=cached_data["status_code"],
                    content=json_body,
                    headers=headers,
                )
            except json.JSONDecodeError:
                pass

        # Fallback to generic response
        return Response(
            status_code=cached_data["status_code"],
            content=body,
            headers=headers,
            media_type=content_type,
        )

    def _create_body_iterator(self, body: bytes):
        """Create async iterator for response body."""

        async def body_iterator():
            yield body

        return body_iterator()

    def _get_timestamp(self) -> str:
        """Get ISO timestamp for caching."""
        from datetime import datetime, timezone

        from faultmaven.utils.serialization import to_json_compatible

        return to_json_compatible(datetime.now(timezone.utc))


def create_idempotency_middleware(
    app: ASGIApp, redis_client=None
) -> IdempotencyMiddleware:
    """Factory function to create idempotency middleware with Redis client."""
    return IdempotencyMiddleware(app, redis_client=redis_client)
