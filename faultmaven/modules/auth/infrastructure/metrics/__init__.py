"""Auth module metrics.

Prometheus metrics for OAuth 2.0 + PKCE authentication, for the request-path
token revocation check (#1478), and for operator case reads (ADR-012 D9).
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
from faultmaven.modules.auth.infrastructure.metrics.operator_read_metrics import (
    OPERATOR_READ_DEPLOYMENTS,
    OPERATOR_READ_SURFACES,
    operator_case_reads_total,
)
from faultmaven.modules.auth.infrastructure.metrics.revocation_metrics import (
    revocation_state_unknown_total,
)

__all__ = [
    # Operator case reads (ADR-012 D9)
    "operator_case_reads_total",
    "OPERATOR_READ_SURFACES",
    "OPERATOR_READ_DEPLOYMENTS",
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
    # Revocation-check telemetry (#1478)
    "revocation_state_unknown_total",
]
