"""OAuth metrics module.

Provides Prometheus metrics for OAuth 2.0 + PKCE authentication.
"""

from faultmaven.modules.auth.infrastructure.metrics.oauth_metrics import (
    OAuthMetricsRecorder,
    oauth_authorization_errors,
    oauth_authorization_requests,
    oauth_code_replay_attempts,
    oauth_codes_expired,
    oauth_codes_generated,
    oauth_invalid_client_attempts,
    oauth_metrics,
    oauth_pkce_verification_failures,
    oauth_redirect_uri_mismatches,
    oauth_refresh_errors,
    oauth_token_exchange_duration,
    oauth_token_exchange_errors,
    oauth_tokens_issued,
    oauth_tokens_refreshed,
    oauth_tokens_revoked,
)

__all__ = [
    "oauth_metrics",
    "OAuthMetricsRecorder",
    # Authorization metrics
    "oauth_authorization_requests",
    "oauth_authorization_errors",
    "oauth_codes_generated",
    # Token issuance metrics
    "oauth_tokens_issued",
    "oauth_token_exchange_duration",
    "oauth_token_exchange_errors",
    # Token refresh metrics
    "oauth_tokens_refreshed",
    "oauth_refresh_errors",
    # Token revocation metrics
    "oauth_tokens_revoked",
    # Security metrics
    "oauth_pkce_verification_failures",
    "oauth_code_replay_attempts",
    "oauth_invalid_client_attempts",
    "oauth_redirect_uri_mismatches",
    "oauth_codes_expired",
]
