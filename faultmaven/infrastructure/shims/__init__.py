"""Infrastructure shims for graceful degradation.

This package provides shim layers for optional enterprise dependencies:
- Observability (Opik): Distributed tracing
- Security (Presidio): PII redaction

Shims enable FaultMaven to run in both:
- Community mode: Zero external dependencies
- Enterprise mode: Full observability and security features

Environment Variables:
    ENABLE_TRACING: Set to 'true' to enable Opik distributed tracing
    ENABLE_PII_REDACTION: Set to 'true' to enable Presidio PII redaction

Usage:
    from faultmaven.infrastructure.shims import track, PIIRedactor

    # Tracing (no-op if Opik not installed or disabled)
    @track("my_operation")
    async def my_function():
        pass

    # PII redaction (pass-through if Presidio not installed or disabled)
    redactor = PIIRedactor()
    safe_text = redactor.redact(user_input)

    # Check status of shims
    from faultmaven.infrastructure.shims import get_tracing_status, get_pii_redaction_status

    tracing = get_tracing_status()
    print(f"Tracing active: {tracing['active']}")

    pii = get_pii_redaction_status()
    print(f"PII redaction available: {pii['would_be_active']}")
"""

from .observability import (
    track,
    get_tracing_status,
    is_tracing_active,
    OPIK_AVAILABLE,
)
from .security import (
    PIIRedactor,
    get_pii_redaction_status,
    PRESIDIO_AVAILABLE,
)

__all__ = [
    # Observability exports
    "track",
    "get_tracing_status",
    "is_tracing_active",
    "OPIK_AVAILABLE",
    # Security exports
    "PIIRedactor",
    "get_pii_redaction_status",
    "PRESIDIO_AVAILABLE",
]
