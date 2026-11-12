"""Prometheus metrics for OAuth 2.0 + PKCE authentication.

This module provides Prometheus metrics for monitoring OAuth authentication flows,
including authorization code generation, token exchange, refresh operations, and
security-related events like PKCE violations and replay attacks.

Metrics follow FaultMaven's bounded cardinality principles:
- NO user_id, session_id, or other high-cardinality labels
- A ``client_id`` label is a configured OAuth client or ``unknown``, and
  ``attempted_client_id`` is always ``unknown``: never the raw request value (#2735)

Usage: record through an ``OAuthMetricsRecorder`` built with the configured
clients; its methods fold every client id through ``client_label``.

    recorder = OAuthMetricsRecorder(allowed_clients=settings.oauth_allowed_clients)
    recorder.record_authorization_request(client_id=request.client_id, success=True)
"""

import logging
from typing import Iterable, Optional

from faultmaven.infrastructure.shims.metrics import create_children, declare_lazy

logger = logging.getLogger(__name__)

# Flag to track if prometheus_client is available
_PROMETHEUS_AVAILABLE = False
_METRICS_INITIALIZED = False

try:
    from prometheus_client import Counter, Histogram

    _PROMETHEUS_AVAILABLE = True
except ImportError:
    logger.warning(
        "prometheus_client not installed. OAuth metrics will be no-ops. "
        "Install with: pip install prometheus-client"
    )

    # Create no-op stub classes if prometheus_client not available
    class Counter:
        """No-op Counter stub"""

        def __init__(self, *args, **kwargs):
            pass

        def labels(self, **kwargs):
            return self

        def inc(self, amount=1):
            pass

    class Histogram:
        """No-op Histogram stub"""

        def __init__(self, *args, **kwargs):
            pass

        def labels(self, **kwargs):
            return self

        def observe(self, amount):
            pass


#: The label of every client id that is not a configured OAuth client.
_UNKNOWN_CLIENT = "unknown"

#: Why the ``client_id`` counters create no children up front: the configured
#: clients are known only when a recorder is built from the settings.
_CLIENT_LABEL_REASON = (
    "client_id is a configured OAuth client or 'unknown', folded by client_label."
)

# ============================================================
# OAuth Authorization Metrics
# ============================================================

oauth_authorization_requests = Counter(
    "oauth_authorization_requests_total",
    "Total number of OAuth authorization code requests",
    ["client_id", "status"],
)
declare_lazy(oauth_authorization_requests, _CLIENT_LABEL_REASON)

oauth_authorization_errors = Counter(
    "oauth_authorization_errors_total",
    "Total number of OAuth authorization errors",
    ["client_id", "error_code"],
)
declare_lazy(oauth_authorization_errors, _CLIENT_LABEL_REASON)

# ============================================================
# Token Issuance Metrics
# ============================================================

#: What every ``OAuthService`` caller of the token-exchange, PKCE, replay and
#: expiry recorders passes: the code's client is not known on those paths.
_CALLER_CLIENT_IDS = (_UNKNOWN_CLIENT,)

oauth_tokens_issued = Counter(
    "oauth_tokens_issued_total",
    "Total number of OAuth tokens issued (access + refresh)",
    ["grant_type", "client_id"],
)
create_children(
    oauth_tokens_issued,
    {"grant_type": ("authorization_code",), "client_id": _CALLER_CLIENT_IDS},
)

oauth_token_exchange_duration = Histogram(
    "oauth_token_exchange_duration_seconds",
    "Duration of OAuth token exchange operations",
    ["grant_type", "status"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

oauth_token_exchange_errors = Counter(
    "oauth_token_exchange_errors_total",
    "Total number of OAuth token exchange errors",
    ["grant_type", "error_code"],
)
# The fixed literals ``OAuthService`` passes to ``record_token_exchange``.
create_children(
    oauth_token_exchange_errors,
    {
        "grant_type": ("authorization_code",),
        "error_code": (
            "INVALID_AUTHORIZATION_CODE",
            "CODE_ALREADY_USED",
            "CODE_EXPIRED",
            "REDIRECT_URI_MISMATCH",
            "PKCE_VERIFICATION_FAILED",
            "CLIENT_MISMATCH",
        ),
    },
)

# ============================================================
# Token Refresh Metrics
# ============================================================

oauth_tokens_refreshed = Counter(
    "oauth_tokens_refreshed_total",
    "Total number of OAuth token refresh operations",
    ["client_id", "status"],
)
declare_lazy(oauth_tokens_refreshed, _CLIENT_LABEL_REASON)

oauth_refresh_errors = Counter(
    "oauth_refresh_errors_total",
    "Total number of OAuth token refresh errors",
    ["client_id", "error_code"],
)
declare_lazy(oauth_refresh_errors, _CLIENT_LABEL_REASON)

# ============================================================
# Token Revocation Metrics
# ============================================================

oauth_tokens_revoked = Counter(
    "oauth_tokens_revoked_total",
    "Total number of OAuth tokens revoked",
    ["token_type"],
)
create_children(oauth_tokens_revoked, {"token_type": ("access_token", "refresh_token")})

# ============================================================
# Security Metrics
# ============================================================

oauth_pkce_verification_failures = Counter(
    "oauth_pkce_verification_failures_total",
    "Total number of PKCE verification failures (potential attacks)",
    ["client_id"],
)
create_children(oauth_pkce_verification_failures, {"client_id": _CALLER_CLIENT_IDS})

oauth_code_replay_attempts = Counter(
    "oauth_code_replay_attempts_total",
    "Total number of authorization code replay attempts detected",
    ["client_id"],
)
create_children(oauth_code_replay_attempts, {"client_id": _CALLER_CLIENT_IDS})

oauth_invalid_client_attempts = Counter(
    "oauth_invalid_client_attempts_total",
    "Total number of requests with invalid client_id",
    ["attempted_client_id"],
)
create_children(
    oauth_invalid_client_attempts, {"attempted_client_id": (_UNKNOWN_CLIENT,)}
)

oauth_redirect_uri_mismatches = Counter(
    "oauth_redirect_uri_mismatches_total",
    "Total number of redirect URI mismatch errors",
    ["client_id"],
)
declare_lazy(oauth_redirect_uri_mismatches, _CLIENT_LABEL_REASON)

# ============================================================
# Code Lifecycle Metrics
# ============================================================

oauth_codes_generated = Counter(
    "oauth_codes_generated_total",
    "Total number of authorization codes generated",
    ["client_id"],
)
declare_lazy(oauth_codes_generated, _CLIENT_LABEL_REASON)

oauth_codes_expired = Counter(
    "oauth_codes_expired_total",
    "Total number of expired authorization code exchanges",
    ["client_id"],
)
create_children(oauth_codes_expired, {"client_id": _CALLER_CLIENT_IDS})


# ============================================================
# Helper Functions
# ============================================================


class OAuthMetricsRecorder:
    """Helper class for recording OAuth metrics with consistent patterns.

    Every client id it labels passes through ``client_label``, so the series
    count is bounded by the configured clients, never by request input (#2735).
    """

    def __init__(self, allowed_clients: Iterable[str] = ()):
        """Initialize metrics recorder.

        Args:
            allowed_clients: The configured OAuth client ids, the only values a
                client label keeps.

        Raises:
            TypeError: *allowed_clients* is a bare ``str``, which would be read
                as its characters.
        """
        if isinstance(allowed_clients, str):
            raise TypeError("allowed_clients must be a collection of ids, not a str")
        self._allowed_clients = frozenset(allowed_clients)
        self.enabled = _PROMETHEUS_AVAILABLE
        if not self.enabled:
            logger.debug("OAuth metrics recorder initialized in no-op mode")

    def client_label(self, client_id: object) -> str:
        """*client_id* when it is a configured client, else ``unknown``. Never raises.

        An exact ``str`` only: a subclass can make hashing raise.
        """
        if type(client_id) is str and client_id in self._allowed_clients:
            return client_id
        return _UNKNOWN_CLIENT

    def record_authorization_request(
        self, client_id: str, success: bool, error_code: Optional[str] = None
    ) -> None:
        """Record OAuth authorization code request.

        Args:
            client_id: OAuth client ID
            success: Whether request succeeded
            error_code: Error code if request failed
        """
        if not self.enabled:
            return

        try:
            client_id = self.client_label(client_id)
            status = "success" if success else "error"
            oauth_authorization_requests.labels(
                client_id=client_id, status=status
            ).inc()

            if error_code:
                oauth_authorization_errors.labels(
                    client_id=client_id, error_code=error_code
                ).inc()

            if success:
                oauth_codes_generated.labels(client_id=client_id).inc()

        except Exception as e:
            logger.error(f"Failed to record authorization request metric: {e}")

    def record_token_exchange(
        self,
        grant_type: str,
        client_id: str,
        duration_seconds: float,
        success: bool,
        error_code: Optional[str] = None,
    ) -> None:
        """Record OAuth token exchange operation.

        Args:
            grant_type: OAuth grant type (authorization_code, refresh_token)
            client_id: OAuth client ID
            duration_seconds: Operation duration in seconds
            success: Whether exchange succeeded
            error_code: Error code if exchange failed
        """
        if not self.enabled:
            return

        try:
            status = "success" if success else "error"

            # Record duration
            oauth_token_exchange_duration.labels(
                grant_type=grant_type, status=status
            ).observe(duration_seconds)

            if success:
                # Record successful token issuance
                oauth_tokens_issued.labels(
                    grant_type=grant_type, client_id=self.client_label(client_id)
                ).inc()
            else:
                # Record error
                if error_code:
                    oauth_token_exchange_errors.labels(
                        grant_type=grant_type, error_code=error_code
                    ).inc()

        except Exception as e:
            logger.error(f"Failed to record token exchange metric: {e}")

    def record_token_refresh(
        self, client_id: str, success: bool, error_code: Optional[str] = None
    ) -> None:
        """Record OAuth token refresh operation.

        Args:
            client_id: OAuth client ID
            success: Whether refresh succeeded
            error_code: Error code if refresh failed
        """
        if not self.enabled:
            return

        try:
            client_id = self.client_label(client_id)
            status = "success" if success else "error"
            oauth_tokens_refreshed.labels(client_id=client_id, status=status).inc()

            if error_code:
                oauth_refresh_errors.labels(
                    client_id=client_id, error_code=error_code
                ).inc()

        except Exception as e:
            logger.error(f"Failed to record token refresh metric: {e}")

    def record_token_revocation(self, token_type: str) -> None:
        """Record OAuth token revocation.

        Args:
            token_type: Type of token revoked (access_token, refresh_token)
        """
        if not self.enabled:
            return

        try:
            oauth_tokens_revoked.labels(token_type=token_type).inc()
        except Exception as e:
            logger.error(f"Failed to record token revocation metric: {e}")

    def record_pkce_failure(self, client_id: str) -> None:
        """Record PKCE verification failure.

        Args:
            client_id: OAuth client ID
        """
        if not self.enabled:
            return

        try:
            oauth_pkce_verification_failures.labels(
                client_id=self.client_label(client_id)
            ).inc()
        except Exception as e:
            logger.error(f"Failed to record PKCE failure metric: {e}")

    def record_code_replay_attempt(self, client_id: str) -> None:
        """Record authorization code replay attempt.

        Args:
            client_id: OAuth client ID
        """
        if not self.enabled:
            return

        try:
            oauth_code_replay_attempts.labels(
                client_id=self.client_label(client_id)
            ).inc()
        except Exception as e:
            logger.error(f"Failed to record code replay attempt metric: {e}")

    def record_invalid_client(self) -> None:
        """Record a request whose client_id is not a configured client.

        The attempted id is never a label: it is request input, so each distinct
        value would be a new series (#2735). The series is ``unknown``.
        """
        if not self.enabled:
            return

        try:
            oauth_invalid_client_attempts.labels(
                attempted_client_id=_UNKNOWN_CLIENT
            ).inc()
        except Exception as e:
            logger.error(f"Failed to record invalid client metric: {e}")

    def record_redirect_uri_mismatch(self, client_id: str) -> None:
        """Record redirect URI mismatch.

        Args:
            client_id: OAuth client ID
        """
        if not self.enabled:
            return

        try:
            oauth_redirect_uri_mismatches.labels(
                client_id=self.client_label(client_id)
            ).inc()
        except Exception as e:
            logger.error(f"Failed to record redirect URI mismatch metric: {e}")

    def record_code_expired(self, client_id: str) -> None:
        """Record expired authorization code exchange attempt.

        Args:
            client_id: OAuth client ID
        """
        if not self.enabled:
            return

        try:
            oauth_codes_expired.labels(client_id=self.client_label(client_id)).inc()
        except Exception as e:
            logger.error(f"Failed to record code expired metric: {e}")
