"""OAuth-specific rate limiting.

Implements rate limiting for OAuth endpoints to prevent:
- Brute force attacks on authorization codes
- Token enumeration attacks
- Denial of service

Limits are keyed on the *resolved* client IP (``client_ip.resolve_client_ip``),
not on the raw socket peer. Behind an ingress every request arrives from the
same peer address, so a socket-peer key puts the whole deployment in one
bucket — the first user to authenticate would 429 everybody else for the
minute. Forwarding headers are believed only when the peer is listed in
``PROTECTION_TRUSTED_PROXIES``, so an unconfigured deployment behaves exactly
as it did before.

Rate limits (per resolved client IP, per minute):
- /authorize: 10 requests (prevent authorization flooding)
- /token: 5 requests (prevent token brute force)
- /revoke: 20 requests (allow normal logout patterns)
"""

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional, Tuple

from fastapi import HTTPException, Request, status

from faultmaven.api.middleware.client_ip import (
    TrustedProxies,
    parse_trusted_proxies,
    resolve_client_ip,
)
from faultmaven.config.protection import get_trusted_proxies
from faultmaven.infrastructure.protection.window_math import (
    quota_frees_at,
    retry_after_seconds,
)
from faultmaven.models.protection import LimitType, RateLimitResult

logger = logging.getLogger(__name__)


class OAuthRateLimiter:
    """In-memory rate limiter for OAuth endpoints.

    Uses sliding window algorithm with per-resolved-client-IP tracking.
    For production with multiple backend instances, use Redis-backed rate limiter.
    """

    def __init__(self, trusted_proxies: Optional[Iterable[str]] = None):
        # Trust policy for forwarding headers, resolved once at construction:
        # an explicit list when the caller supplies one, the environment
        # otherwise. Pinned thereafter — re-reading per request would make the
        # limit key depend on a mutable global.
        #
        # Reading the environment here is safe even though the module-level
        # singleton is built at *import* time: ``main`` runs ``load_dotenv()``
        # at its own import, before any router (and therefore this module) is
        # imported, so `.env` has already reached ``os.environ``. Post-boot the
        # value does not change, which is why ``reset_rate_limiter`` — the test
        # seam — is the only thing that re-reads it.
        self.trusted_proxies: TrustedProxies = parse_trusted_proxies(
            trusted_proxies if trusted_proxies is not None else get_trusted_proxies()
        )

        # Store: {(ip, endpoint): [(timestamp1, timestamp2, ...)]}
        self._requests: Dict[Tuple[str, str], list[float]] = defaultdict(list)

        # Rate limits per endpoint (requests per minute)
        self._limits = {
            "/authorize": 10,  # Prevent authorization flooding
            "/token": 5,  # Prevent token brute force
            "/revoke": 20,  # Allow normal logout patterns
            "/sso/login": 10,  # Prevent state-store flooding
            "/sso/callback": 10,  # Prevent state/code guessing via the IdP leg
            "/sso/exchange": 5,  # Prevent completion-code brute force
        }

        # Window size in seconds
        self._window_seconds = 60

        # Last cleanup time
        self._last_cleanup = time.time()
        self._cleanup_interval = 300  # 5 minutes

    def _cleanup_old_entries(self):
        """Remove entries older than window size."""
        now = time.time()

        # Only cleanup every 5 minutes
        if now - self._last_cleanup < self._cleanup_interval:
            return

        cutoff = now - self._window_seconds
        for key in list(self._requests.keys()):
            # Filter out old timestamps
            self._requests[key] = [ts for ts in self._requests[key] if ts > cutoff]

            # Remove empty entries
            if not self._requests[key]:
                del self._requests[key]

        self._last_cleanup = now

    async def check_rate_limit(self, request: Request, endpoint_name: str) -> None:
        """Check if request is within rate limit.

        This limiter is the *tightest* quota an OAuth/SSO caller is under — 5 to
        10 per minute against the general limits' hundreds — so it is also the
        one such a caller needs to pace against. Both outcomes therefore publish
        its state, and both publish it the way the Redis-backed enforcer does:

        - A refusal carries the full quartet on the 429 it raises. ``Retry-After``
          alone tells a client when to come back but not what it came up
          against, so it cannot tell a tight endpoint limit from the roomy global
          one and cannot pace itself before the next refusal.
        - An allowed request contributes a ``RateLimitResult`` to
          ``request.state.rate_limit_results``, the list ``RateLimitMiddleware``
          picks the least-remaining limit out of on the way back. That is what
          makes the OAuth limit visible on a 2xx: without it the served response
          advertised only the roomy limits this caller is nowhere near, while
          the one it is five requests from was never mentioned.

        The header authority stays single: the component that enforced a limit
        is the one that publishes it, here as everywhere.

        Args:
            request: FastAPI request object
            endpoint_name: Endpoint identifier ("/authorize", "/token", "/revoke")

        Raises:
            HTTPException: 429 if rate limit exceeded
        """
        # Get client IP. Not the raw socket peer: behind an ingress that is one
        # address for every client, and this limit would then be shared.
        client_ip = resolve_client_ip(request, self.trusted_proxies)

        # Get rate limit for this endpoint
        limit = self._limits.get(endpoint_name, 10)  # Default 10/min

        # Get current timestamp
        now = time.time()
        cutoff = now - self._window_seconds

        # Get request history for this IP + endpoint
        key = (client_ip, endpoint_name)
        requests = self._requests[key]

        # Remove old requests (sliding window)
        requests = [ts for ts in requests if ts > cutoff]
        self._requests[key] = requests

        # The window is sliding, so the instant quota frees is when the oldest
        # surviving timestamp ages out — not a flat minute from now.
        # ``requests`` is pruned and in arrival order, so its first element is
        # that timestamp; an empty list means nothing is left to age out and the
        # entry that is about to go in becomes the oldest. The shared helper is
        # the same one the Redis-backed limiter derives from, so the two
        # enforcers cannot drift apart on the formula.
        frees_at = quota_frees_at(
            requests[0] if requests else None, self._window_seconds, now
        )

        # Check if limit exceeded
        if len(requests) >= limit:
            logger.warning(
                f"Rate limit exceeded for {client_ip} on {endpoint_name}: "
                f"{len(requests)}/{limit} requests in last {self._window_seconds}s"
            )
            # A hardcoded 60 told a caller one second from quota to wait sixty,
            # which is the difference between a client that backs off correctly
            # and one that gives up.
            retry_after = retry_after_seconds(frees_at, now)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Maximum {limit} requests per minute.",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    # The same instant ``Retry-After`` is derived from, as a
                    # timestamp — one measurement, two renderings.
                    "X-RateLimit-Reset": str(int(frees_at)),
                },
            )

        # The count this request makes, read BEFORE the insert. ``requests`` is
        # the same list object ``self._requests[key]`` holds — the prune
        # rebinds it there — so appending mutates it, and any ``len(requests)``
        # taken afterwards already includes the new entry.
        current_count = len(requests) + 1

        # Add current request timestamp
        self._requests[key].append(now)

        # Periodic cleanup
        self._cleanup_old_entries()

        # ``current_count`` includes the request that just went in, matching the
        # Lua script's allowed-path return of ``count + 1``. Both enforcers
        # therefore mean the same thing by it, so ``limit - current_count`` is
        # the remaining quota on either — the header path subtracts them without
        # knowing which limiter produced the result.
        self._publish_allowed(
            request,
            limit=limit,
            current_count=current_count,
            frees_at=frees_at,
        )

        logger.debug(
            f"Rate limit check passed for {client_ip} on {endpoint_name}: "
            f"{current_count}/{limit} requests"
        )

    @staticmethod
    def _publish_allowed(
        request: Request, *, limit: int, current_count: int, frees_at: float
    ) -> None:
        """Offer this limit's state to the served-response header path.

        ``RateLimitMiddleware`` creates ``request.state.rate_limit_results``
        before route dependencies run and reads it after the route returns, so
        an entry appended here is seen when it picks the tightest limit to
        advertise. The attribute is read with ``getattr`` and never created:
        its absence means the middleware is not in the stack — reduced test
        stacks, or any app mounting these routers alone — and manufacturing the
        list there would build a collection nothing ever reads.
        """
        results = getattr(request.state, "rate_limit_results", None)
        if results is None:
            return

        results.append(
            RateLimitResult(
                allowed=True,
                limit_type=LimitType.OAUTH,
                current_count=current_count,
                limit=limit,
                reset_time=datetime.fromtimestamp(frees_at, tz=timezone.utc),
            )
        )


# Global rate limiter instance (singleton)
_oauth_rate_limiter = OAuthRateLimiter()


def reset_rate_limiter():
    """Reset rate limiter state (for testing).

    Clears all request history to prevent test interference, and re-reads the
    trust list from the environment so a test that varies
    ``PROTECTION_TRUSTED_PROXIES`` gets the value it just set rather than the
    one construction pinned. Unconditional, because the singleton is
    env-sourced by construction — it is never handed an explicit list.
    """
    _oauth_rate_limiter._requests.clear()
    _oauth_rate_limiter._last_cleanup = time.time()
    _oauth_rate_limiter.trusted_proxies = parse_trusted_proxies(get_trusted_proxies())


def trusted_proxy_networks() -> TrustedProxies:
    """The trust list the OAuth/SSO limiters key on.

    Exposed so that anything recording *who* a request came from — the SSO
    JIT-provisioning audit trail, in particular — resolves the address through
    the same list the limiter enforces on. Two independently resolved lists
    could drift, and an audit row naming a different address than the limit was
    applied to is worse than either alone.
    """
    return _oauth_rate_limiter.trusted_proxies


async def require_oauth_rate_limit_authorize(request: Request) -> None:
    """Rate limiting dependency for /authorize endpoint.

    Usage:
        @router.get("/authorize", dependencies=[Depends(require_oauth_rate_limit_authorize)])
        async def authorize(...):
            ...
    """
    await _oauth_rate_limiter.check_rate_limit(request, "/authorize")


async def require_oauth_rate_limit_token(request: Request) -> None:
    """Rate limiting dependency for /token endpoint.

    Usage:
        @router.post("/token", dependencies=[Depends(require_oauth_rate_limit_token)])
        async def token(...):
            ...
    """
    await _oauth_rate_limiter.check_rate_limit(request, "/token")


async def require_oauth_rate_limit_revoke(request: Request) -> None:
    """Rate limiting dependency for /revoke endpoint.

    Usage:
        @router.post("/revoke", dependencies=[Depends(require_oauth_rate_limit_revoke)])
        async def revoke(...):
            ...
    """
    await _oauth_rate_limiter.check_rate_limit(request, "/revoke")


async def require_sso_rate_limit_login(request: Request) -> None:
    """Rate limiting dependency for GET /auth/sso/login (ADR-015)."""
    await _oauth_rate_limiter.check_rate_limit(request, "/sso/login")


async def require_sso_rate_limit_callback(request: Request) -> None:
    """Rate limiting dependency for GET /auth/sso/callback (ADR-015)."""
    await _oauth_rate_limiter.check_rate_limit(request, "/sso/callback")


async def require_sso_rate_limit_exchange(request: Request) -> None:
    """Rate limiting dependency for POST /auth/sso/exchange (ADR-015)."""
    await _oauth_rate_limiter.check_rate_limit(request, "/sso/exchange")
