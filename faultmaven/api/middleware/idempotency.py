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

        # Scope the cache to the caller. A request that carries no caller
        # identity at all must not participate in idempotency in either
        # direction — it may neither read nor write the cache. Anonymous
        # callers would otherwise all share one bucket, and because the cache
        # lookup happens before ``call_next`` (route-level ``Depends`` auth
        # never runs on a hit) an unauthenticated request could be served an
        # authenticated caller's response body.
        caller_identity = self._caller_identity(request)
        if caller_identity is None:
            return await call_next(request)

        try:
            # Resolve the Redis client lazily (after startup has populated
            # app.state). Kept inside the try so any failure during resolution
            # degrades gracefully (process the request) rather than 500-ing.
            self._ensure_redis(request)
            if self.redis_client is None:
                return await call_next(request)

            # Create cache key
            body_fingerprint = await self._body_fingerprint(request)
            cache_key = self._create_cache_key(
                idempotency_key, request, caller_identity, body_fingerprint
            )

            # Check for existing response
            cached_response = await self._get_cached_response(cache_key)
            if cached_response:
                logger.info(
                    f"Returning cached response for idempotency key: {idempotency_key}"
                )
                return self._create_response_from_cache(cached_response)

            # Process request normally
            response = await call_next(request)

            # Cache successful responses (2xx status codes)
            if 200 <= response.status_code < 300:
                await self._cache_response(cache_key, response, idempotency_key)

            return response

        except Exception as e:
            logger.error(f"Error in idempotency middleware: {e}")
            # Continue processing on middleware errors
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
        """Whether this path is structurally excluded from idempotency."""
        return any(marker in path for marker in EXCLUDED_PATH_MARKERS)

    def _caller_identity(self, request: Request) -> Optional[str]:
        """Hash the raw credential material that identifies the caller.

        Returns ``None`` when the request carries no caller identity at all,
        which the dispatcher treats as "do not participate in idempotency" —
        the same fail-closed shape as ``DeduplicationMiddleware`` returning a
        ``None`` request hash when there is no session id.

        The identity is derived from the *raw* credential on the wire (the
        ``Authorization`` header value and ``X-Session-ID``), never from a
        decoded JWT claim. This middleware runs before any signature
        verification, so a claim such as ``sub`` is attacker-controlled at this
        point: keying on it would let a forged, unverified token select which
        caller's cache bucket to land in. The raw string cannot be forged into
        another caller's bucket without already possessing that caller's
        credential. It is hashed so no credential material reaches Redis keys
        or logs.
        """
        authorization = request.headers.get("Authorization")
        session_id = request.headers.get("X-Session-ID")

        if not authorization and not session_id:
            return None

        material = f"{authorization or ''}\x00{session_id or ''}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    async def _body_fingerprint(self, request: Request) -> str:
        """Fingerprint the request body so a reused key cannot swap payloads.

        Buffering a body inside ``BaseHTTPMiddleware`` is only safe because
        Starlette wraps the request in ``_CachedRequest``: once ``body()`` has
        been awaited, ``wrapped_receive`` replays the cached bytes to the
        downstream app. We probe for that contract explicitly and fall back to
        the unfingerprinted sentinel if it is ever absent, so a Starlette change
        can only weaken this check — never starve a downstream route of its body.

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
        body_fingerprint: str,
    ) -> str:
        """Create Redis cache key scoped to caller, route and payload.

        The key must not be reachable by anyone but the caller that created it,
        so caller identity is part of the hashed material rather than an
        advisory extra.
        """
        method_path = f"{request.method}:{request.url.path}"
        combined = "|".join(
            [idempotency_key, method_path, caller_identity, body_fingerprint]
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
        self, cache_key: str, response: Response, idempotency_key: str
    ):
        """Cache response in Redis with TTL."""
        try:
            # Read response body
            body = b""
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
                "cached_at": self._get_timestamp(),
            }

            # Store in Redis with TTL
            await self.redis_client.setex(
                cache_key, self.ttl_seconds, json.dumps(cache_data)
            )

            logger.info(f"Cached response for idempotency key: {idempotency_key}")

            # Recreate response with same body for return
            response.body_iterator = self._create_body_iterator(body)

        except Exception as e:
            logger.error(f"Error caching response: {e}")

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
