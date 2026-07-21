"""Redis-backed single-use ephemeral store for the SSO login flow (ADR-015).

Holds two short-lived, single-use JSON values:

* login ``state`` — the CSRF token binding ``/auth/sso/login`` to the callback;
* a completion ``code`` — hands the minted FaultMaven session from the callback
  to the dashboard without ever placing tokens in a URL.

Both are consumed atomically with ``GETDEL``, so a replay (double callback, a
leaked code re-submitted) finds nothing. Expiry is enforced by Redis TTL, so a
value that is never consumed simply vanishes.
"""

from __future__ import annotations

import json
from typing import Any

_STATE_PREFIX = "sso:state:"
_LOGIN_PREFIX = "sso:login:"


class SSOEphemeralStore:
    """Single-use store for SSO ``state`` tokens and completion ``code`` payloads.

    Backed by an async Redis client (real Redis in cloud, in-process FakeRedis in
    standalone). The store is only constructed on the configured cloud SSO path.
    """

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    # -- state (login CSRF token) ------------------------------------------- #

    async def put_state(self, state: str, payload: dict, ttl_seconds: int) -> None:
        """Store the login ``state`` payload under a TTL (single-use)."""
        await self._put(_STATE_PREFIX + state, payload, ttl_seconds)

    async def consume_state(self, state: str) -> dict | None:
        """Atomically fetch-and-delete the ``state`` payload, or None if absent."""
        return await self._consume(_STATE_PREFIX + state)

    # -- completion code (callback -> dashboard hand-off) ------------------- #

    async def put_login(self, code: str, payload: dict, ttl_seconds: int) -> None:
        """Store the completion ``code`` payload under a TTL (single-use)."""
        await self._put(_LOGIN_PREFIX + code, payload, ttl_seconds)

    async def consume_login(self, code: str) -> dict | None:
        """Atomically fetch-and-delete the completion payload, or None if absent."""
        return await self._consume(_LOGIN_PREFIX + code)

    # -- internals ---------------------------------------------------------- #

    async def _put(self, key: str, payload: dict, ttl_seconds: int) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")
        await self._redis.setex(key, ttl_seconds, json.dumps(payload))

    async def _consume(self, key: str) -> dict | None:
        raw = await self._redis.getdel(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
