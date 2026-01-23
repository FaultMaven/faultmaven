"""OAuth-specific rate limiting.

Implements rate limiting for OAuth endpoints to prevent:
- Brute force attacks on authorization codes
- Token enumeration attacks
- Denial of service

Rate limits:
- /authorize: 10 requests per minute per IP (prevent authorization flooding)
- /token: 5 requests per minute per IP (prevent token brute force)
- /revoke: 20 requests per minute per IP (allow normal logout patterns)
"""

import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Tuple

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)


class OAuthRateLimiter:
    """In-memory rate limiter for OAuth endpoints.

    Uses sliding window algorithm with per-IP tracking.
    For production with multiple backend instances, use Redis-backed rate limiter.
    """

    def __init__(self):
        # Store: {(ip, endpoint): [(timestamp1, timestamp2, ...)]}
        self._requests: Dict[Tuple[str, str], list[float]] = defaultdict(list)

        # Rate limits per endpoint (requests per minute)
        self._limits = {
            "/authorize": 10,  # Prevent authorization flooding
            "/token": 5,  # Prevent token brute force
            "/revoke": 20,  # Allow normal logout patterns
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

        Args:
            request: FastAPI request object
            endpoint_name: Endpoint identifier ("/authorize", "/token", "/revoke")

        Raises:
            HTTPException: 429 if rate limit exceeded
        """
        # Get client IP
        client_ip = request.client.host if request.client else "unknown"

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

        # Check if limit exceeded
        if len(requests) >= limit:
            logger.warning(
                f"Rate limit exceeded for {client_ip} on {endpoint_name}: "
                f"{len(requests)}/{limit} requests in last {self._window_seconds}s"
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Maximum {limit} requests per minute.",
                headers={"Retry-After": "60"},
            )

        # Add current request timestamp
        self._requests[key].append(now)

        # Periodic cleanup
        self._cleanup_old_entries()

        logger.debug(
            f"Rate limit check passed for {client_ip} on {endpoint_name}: "
            f"{len(requests)+1}/{limit} requests"
        )


# Global rate limiter instance (singleton)
_oauth_rate_limiter = OAuthRateLimiter()


def reset_rate_limiter():
    """Reset rate limiter state (for testing).

    This clears all request history to prevent test interference.
    """
    _oauth_rate_limiter._requests.clear()
    _oauth_rate_limiter._last_cleanup = time.time()


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
