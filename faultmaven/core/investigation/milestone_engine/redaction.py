"""Whether PII redaction runs at the engine level, read from the case-scoped sanitizer the owner passes at call time."""


def _should_redact(sanitizer) -> bool:
    """Determine whether PII redaction should be applied at the engine level.

    Checks SANITIZE_PII setting. Returns False when no sanitizer is
    configured (redaction disabled at DI level).
    """
    if not sanitizer:
        return False

    from faultmaven.config.settings import get_settings

    return get_settings().protection.sanitize_pii
