"""
Protection system data models

This module defines the data structures used by the client protection system
for rate limiting, request deduplication, and timeout management.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from faultmaven.utils.serialization import to_json_compatible


class ProtectionType(str, Enum):
    """Types of protection mechanisms"""

    RATE_LIMIT = "rate_limit"
    DEDUPLICATION = "deduplication"
    TIMEOUT = "timeout"


class LimitType(str, Enum):
    """Types of rate limits"""

    GLOBAL = "global"
    PER_SESSION = "per_session"
    PER_ENDPOINT = "per_endpoint"
    PER_SESSION_HOURLY = "per_session_hourly"
    # Cheap read traffic — SPA navigation — is metered in its own pair of
    # buckets rather than in ``per_session``/``per_session_hourly``. Sharing the
    # write buckets is what made a burst of GETs refuse the next POST turn; the
    # split is what keeps a read flood out of the quota that protects LLM
    # compute. See ``RateLimitMiddleware._check_rate_limits``.
    PER_SESSION_READ = "per_session_read"
    PER_SESSION_READ_HOURLY = "per_session_read_hourly"
    TITLE_GENERATION = "title_generation"
    # The OAuth/SSO endpoint limiter in ``modules/auth/api/rate_limiting.py``.
    # It enforces a tighter quota (5–10/min) than any of the limits above, so a
    # caller told only about the roomy ones would pace itself against a limit it
    # is nowhere near while the one it is actually hitting stays invisible.
    # Naming it here is what lets its result join the others the served-response
    # header path picks the tightest of.
    OAUTH = "oauth"


class ProtectionResult(str, Enum):
    """Results of protection checks"""

    ALLOWED = "allowed"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass
class RateLimitConfig:
    """Configuration for a specific rate limit"""

    requests: int
    window: int  # seconds
    penalty_multiplier: float = 2.0
    enabled: bool = True


@dataclass
class DeduplicationConfig:
    """Configuration for request deduplication"""

    ttl: int  # seconds
    enabled: bool = True


@dataclass
class TimeoutConfig:
    """Configuration for timeout management"""

    agent_total: int = 60  # seconds
    agent_phase: int = 45  # seconds
    llm_call: int = 30  # seconds
    emergency_shutdown: int = 90  # seconds
    enabled: bool = True


class ProtectionSettings(BaseModel):
    """Complete protection system configuration"""

    # Master protection toggle
    enabled: bool = True

    # Individual protection toggles
    rate_limiting_enabled: bool = True
    deduplication_enabled: bool = True
    timeout_management_enabled: bool = True

    # Rate limiting configuration
    rate_limits: Dict[str, RateLimitConfig] = Field(
        default_factory=lambda: {
            "global": RateLimitConfig(requests=1000, window=60),
            "per_session": RateLimitConfig(requests=20, window=60),
            "per_session_hourly": RateLimitConfig(requests=100, window=3600),
            "per_session_read": RateLimitConfig(requests=240, window=60),
            "per_session_read_hourly": RateLimitConfig(requests=3000, window=3600),
            "title_generation": RateLimitConfig(requests=1, window=300),
            "agent_query": RateLimitConfig(requests=5, window=60),
        }
    )

    # Deduplication configuration
    deduplication: Dict[str, DeduplicationConfig] = Field(
        default_factory=lambda: {"default": DeduplicationConfig(ttl=30)}
    )

    # Timeout configuration
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)

    # Error handling
    fail_open_on_redis_error: bool = True
    fail_open_on_timeout_error: bool = False

    # Development/debugging
    debug_protection: bool = False
    protection_bypass_headers: List[str] = Field(default_factory=list)

    # Proxies whose ``X-Forwarded-For`` header may be believed when resolving
    # the client address a limit is keyed on. Addresses or CIDRs. (``X-Real-IP``
    # is never believed — it has no chain, so nothing separates what the proxy
    # wrote from what the caller sent.)
    #
    # Empty — the default — means no forwarding header is ever honoured and
    # every limit keys on the socket peer. That is the only safe default:
    # honouring the headers unconditionally lets a caller rotate the header and
    # draw a fresh quota per request, which is what made the ``global`` limit
    # (the only one covering unauthenticated traffic) evadable. A deployment
    # behind an ingress must set this to the ingress range, or every client
    # collapses onto one bucket. See ``api/middleware/client_ip.py``.
    trusted_proxies: List[str] = Field(default_factory=list)

    # Redis configuration.
    # ``None`` means "resolve centrally via RedisClientFactory" — the normal
    # case, and the only one that picks up a discretely-configured
    # ``REDIS_PASSWORD``. A value is set only when an operator configures
    # ``REDIS_URL`` explicitly, the one case where a complete URL genuinely is
    # the configured source.
    redis_url: Optional[str] = None
    redis_key_prefix: str = "fm:protection"

    model_config = ConfigDict(
        # Allow enum values
        use_enum_values=True,
    )


@dataclass(frozen=True)
class RateLimitSpec:
    """One window to weigh as part of an all-or-nothing rate limit decision.

    A request is usually subject to several windows at once (an address-keyed
    global limit, a session-keyed per-minute and hourly pair). They are passed
    together rather than checked one at a time so that a refusal by any of them
    consumes quota in none of them — see ``_WINDOWS_SCRIPT``.

    Order is precedence: the first window to refuse is the one the client is
    told about.
    """

    key: str
    limit_type: LimitType


@dataclass
class RateLimitResult:
    """Result of a rate limit check"""

    allowed: bool
    limit_type: LimitType
    current_count: int
    limit: int
    retry_after: Optional[int] = None
    reset_time: Optional[datetime] = None
    # What ``X-RateLimit-Policy`` should name, when the limit type alone is not
    # the identity. Left ``None`` by every enforcer whose type maps to exactly
    # one bucket — the header path then derives the name from ``limit_type``.
    # The OAuth limiter sets it, because six endpoints with different limits
    # share ``LimitType.OAUTH`` and a client cannot pace against a token that
    # names all of them.
    policy: Optional[str] = None


class ProtectionError(Exception):
    """Base class for protection system errors"""

    def __init__(self, message: str, error_code: str = "", correlation_id: str = ""):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.correlation_id = correlation_id
        self.timestamp = datetime.now(timezone.utc)


class RateLimitError(ProtectionError):
    """Rate limit exceeded error.

    Carries the ``reset_time`` the limiter measured, not just the wait derived
    from it. The renderer used to re-derive the instant as
    ``time.time() + retry_after``, which reads the clock a second time — after
    the check, the raise and the response construction — so the timestamp a
    client received drifted from the one the limiter actually computed. Two
    numbers that are meant to name the same instant have to come from one
    measurement. ``None`` means the limiter did not measure one, and an
    unmeasured value is omitted rather than invented.
    """

    def __init__(
        self,
        retry_after: Optional[int],
        limit_type: str,
        current_count: int,
        limit: int,
        correlation_id: str = "",
        reset_time: Optional[datetime] = None,
    ):
        # The message obeys the same contract as the headers: a wait nobody
        # measured is left out of the sentence rather than rendered as the word
        # "None" for a client to read as a duration. The header path already
        # omits it; the body is as much the wire as the header is.
        message = f"Rate limit exceeded: {current_count}/{limit} requests."
        if retry_after is not None:
            message += f" Retry after {retry_after} seconds."
        super().__init__(message, "RATE_LIMIT_EXCEEDED", correlation_id)
        self.retry_after = retry_after
        self.limit_type = limit_type
        self.current_count = current_count
        self.limit = limit
        self.reset_time = reset_time


class DuplicateRequestError(ProtectionError):
    """Duplicate request detected error"""

    def __init__(
        self, original_timestamp: datetime, ttl_remaining: int, correlation_id: str = ""
    ):
        message = f"Duplicate request detected. Original request at {original_timestamp}. TTL: {ttl_remaining}s."
        super().__init__(message, "DUPLICATE_REQUEST", correlation_id)
        self.original_timestamp = original_timestamp
        self.ttl_remaining = ttl_remaining


class TimeoutError(ProtectionError):
    """Operation timeout error"""

    def __init__(
        self, operation: str, timeout_duration: float, correlation_id: str = ""
    ):
        message = f"Operation '{operation}' timed out after {timeout_duration} seconds."
        super().__init__(message, "OPERATION_TIMEOUT", correlation_id)
        self.operation = operation
        self.timeout_duration = timeout_duration


@dataclass
class ProtectionErrorResponse:
    """Standardized error response for protection violations"""

    error_type: str
    message: str
    retry_after: Optional[int] = None
    error_code: str = ""
    correlation_id: str = ""
    timestamp: str = ""
    suggestions: List[str] = field(default_factory=list)

    @classmethod
    def from_rate_limit_error(cls, error: RateLimitError) -> "ProtectionErrorResponse":
        """Create response from rate limit error"""
        return cls(
            error_type="rate_limit_exceeded",
            message=error.message,
            retry_after=error.retry_after,
            error_code=error.error_code,
            correlation_id=error.correlation_id,
            timestamp=to_json_compatible(error.timestamp),
            suggestions=[
                "Wait for the specified retry period",
                "Reduce request frequency",
                "Contact support if this limit seems incorrect",
            ],
        )

    @classmethod
    def from_duplicate_error(
        cls, error: DuplicateRequestError
    ) -> "ProtectionErrorResponse":
        """Create response from duplicate request error"""
        return cls(
            error_type="duplicate_request",
            message=error.message,
            retry_after=error.ttl_remaining,
            error_code=error.error_code,
            correlation_id=error.correlation_id,
            timestamp=to_json_compatible(error.timestamp),
            suggestions=[
                "Avoid sending identical requests rapidly",
                "Check for client-side loops or bugs",
                "Wait for the duplicate detection window to expire",
            ],
        )

    @classmethod
    def from_timeout_error(cls, error: TimeoutError) -> "ProtectionErrorResponse":
        """Create response from timeout error"""
        return cls(
            error_type="operation_timeout",
            message=error.message,
            error_code=error.error_code,
            correlation_id=error.correlation_id,
            timestamp=to_json_compatible(error.timestamp),
            suggestions=[
                "Simplify your query to reduce processing time",
                "Try breaking complex requests into smaller parts",
                "Contact support if timeouts persist",
            ],
        )


@dataclass
class ProtectionMetrics:
    """Metrics collected by the protection system"""

    # Rate limiting metrics
    rate_limit_checks: int = 0
    rate_limit_blocks: int = 0
    rate_limit_errors: int = 0

    # Deduplication metrics
    dedup_checks: int = 0
    dedup_blocks: int = 0
    dedup_errors: int = 0

    # Timeout metrics
    timeout_checks: int = 0
    timeout_blocks: int = 0
    timeout_errors: int = 0

    # Performance metrics
    avg_check_duration: float = 0.0
    max_check_duration: float = 0.0

    # System metrics
    redis_errors: int = 0
    memory_usage: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert metrics to dictionary for serialization"""
        return {
            "rate_limiting": {
                "checks": self.rate_limit_checks,
                "blocks": self.rate_limit_blocks,
                "errors": self.rate_limit_errors,
                "block_rate": self.rate_limit_blocks / max(self.rate_limit_checks, 1),
            },
            "deduplication": {
                "checks": self.dedup_checks,
                "blocks": self.dedup_blocks,
                "errors": self.dedup_errors,
                "block_rate": self.dedup_blocks / max(self.dedup_checks, 1),
            },
            "timeouts": {
                "checks": self.timeout_checks,
                "blocks": self.timeout_blocks,
                "errors": self.timeout_errors,
                "block_rate": self.timeout_blocks / max(self.timeout_checks, 1),
            },
            "performance": {
                "avg_check_duration_ms": self.avg_check_duration * 1000,
                "max_check_duration_ms": self.max_check_duration * 1000,
            },
            "system": {
                "redis_errors": self.redis_errors,
                "memory_usage_bytes": self.memory_usage,
            },
        }
