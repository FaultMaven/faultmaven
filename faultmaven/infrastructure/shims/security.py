"""Security shim for PII redaction.

Provides graceful degradation for Presidio PII redaction:
- If Presidio installed + ENABLE_PII_REDACTION=true: Use Presidio
- Otherwise: Pass-through (return text unchanged)

Usage:
    from faultmaven.infrastructure.shims.security import PIIRedactor

    redactor = PIIRedactor()
    safe_text = redactor.redact("My SSN is 123-45-6789")
"""

import os
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)

# Feature detection
try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine
    PRESIDIO_AVAILABLE = True
    logger.info("Presidio PII redaction library available")
except ImportError:
    PRESIDIO_AVAILABLE = False
    AnalyzerEngine = None  # type: ignore
    AnonymizerEngine = None  # type: ignore
    logger.debug("Presidio not installed - PII redaction disabled")


def _is_pii_redaction_enabled() -> bool:
    """Check if PII redaction is enabled via environment variable."""
    return os.getenv("ENABLE_PII_REDACTION", "false").lower() == "true"


class PIIRedactor:
    """
    PII redaction with graceful degradation.

    If Presidio is available and ENABLE_PII_REDACTION=true, performs PII redaction.
    Otherwise, passes text through unchanged.

    Attributes:
        active: Whether PII redaction is currently active
        pii_enabled: Whether PII redaction is enabled via environment

    Example:
        redactor = PIIRedactor()

        # Redacts PII if enabled, otherwise returns original text
        safe_text = redactor.redact("Contact me at john@example.com")

        # Check if redaction is active
        if redactor.active:
            print("PII redaction is protecting your data")
    """

    def __init__(self) -> None:
        """Initialize PII redactor."""
        self.pii_enabled = _is_pii_redaction_enabled()
        self._analyzer: Optional[object] = None
        self._anonymizer: Optional[object] = None
        self.active = False
        self._initialization_error: Optional[str] = None

        if PRESIDIO_AVAILABLE and self.pii_enabled:
            try:
                self._analyzer = AnalyzerEngine()
                self._anonymizer = AnonymizerEngine()
                self.active = True
                logger.info("PII redaction active (Presidio)")
            except Exception as e:
                logger.warning(f"Failed to initialize Presidio: {e}")
                self._initialization_error = str(e)
                self._analyzer = None
                self._anonymizer = None
                self.active = False
        else:
            reason = []
            if not PRESIDIO_AVAILABLE:
                reason.append("Presidio not installed")
            if not self.pii_enabled:
                reason.append("ENABLE_PII_REDACTION=false")
            logger.debug(f"PII redaction disabled ({', '.join(reason)})")

    def redact(
        self,
        text: str,
        entities: Optional[List[str]] = None,
        language: str = "en"
    ) -> str:
        """
        Redact PII from text.

        Args:
            text: Text to redact PII from
            entities: Optional list of entity types to redact
                     (e.g., ["EMAIL_ADDRESS", "PHONE_NUMBER", "US_SSN"])
                     If None, all recognized entities are redacted.
            language: Language code (default: "en")

        Returns:
            Redacted text if PII redaction active, original text otherwise

        Note:
            This method implements "fail-open" behavior: if an error occurs
            during redaction, the original text is returned to prevent
            application crashes.
        """
        if not self.active or not text:
            return text

        try:
            # Analyze text for PII
            analyzer_results = self._analyzer.analyze(  # type: ignore
                text=text,
                entities=entities,
                language=language
            )

            if not analyzer_results:
                # No PII found
                return text

            # Anonymize detected PII
            anonymized_result = self._anonymizer.anonymize(  # type: ignore
                text=text,
                analyzer_results=analyzer_results
            )

            logger.debug(f"PII redacted: {len(analyzer_results)} entities found")
            return anonymized_result.text

        except Exception as e:
            logger.error(f"PII redaction failed: {e}")
            # On error, return original text (fail-open)
            return text

    def get_status(self) -> dict:
        """
        Get current PII redaction status for diagnostics.

        Returns:
            Dictionary with PII redaction configuration status:
            - presidio_available: Whether the Presidio library is installed
            - pii_redaction_enabled: Whether ENABLE_PII_REDACTION env var is true
            - active: Whether PII redaction is actually active
            - initialization_error: Error message if initialization failed
        """
        return {
            "presidio_available": PRESIDIO_AVAILABLE,
            "pii_redaction_enabled": self.pii_enabled,
            "active": self.active,
            "initialization_error": self._initialization_error,
        }

    def is_active(self) -> bool:
        """
        Check if PII redaction is currently active.

        Returns:
            True if Presidio is initialized and ready to redact
        """
        return self.active


def get_pii_redaction_status() -> dict:
    """
    Get global PII redaction availability status.

    This is a module-level function that checks configuration without
    needing to instantiate a PIIRedactor.

    Returns:
        Dictionary with:
        - presidio_available: Whether the Presidio library is installed
        - pii_redaction_enabled: Whether ENABLE_PII_REDACTION env var is true
        - would_be_active: Whether a PIIRedactor would be active if instantiated
    """
    pii_enabled = _is_pii_redaction_enabled()
    return {
        "presidio_available": PRESIDIO_AVAILABLE,
        "pii_redaction_enabled": pii_enabled,
        "would_be_active": PRESIDIO_AVAILABLE and pii_enabled,
    }
