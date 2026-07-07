"""redaction.py

Purpose: Sanitize sensitive information

Requirements:
--------------------------------------------------------------------------------
• Implement DataSanitizer class
• Use K8s Presidio microservice for PII detection
• Apply custom regex patterns for secrets

Key Components:
--------------------------------------------------------------------------------
  class DataSanitizer: sanitize(text: str) -> str
  CUSTOM_PATTERNS: List[re.Pattern]

Technology Stack:
--------------------------------------------------------------------------------
K8s Presidio microservice, HTTP requests, regex

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Cost-Efficiency: Use semantic caching
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import asyncio
import hashlib
import re
from typing import Any, Dict, List, Optional

import requests

from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.models.interfaces import ISanitizer


class DataSanitizer(BaseExternalClient, ISanitizer):
    """Sanitizes sensitive information from text data

    Implements ISanitizer interface for privacy-first data processing.
    Supports both Presidio-based PII detection and custom regex patterns.
    """

    def __init__(self, settings: Optional["FaultMavenSettings"] = None):  # noqa: F821
        """Initialize the DataSanitizer with optional unified settings.

        If settings are provided and indicate skip_service_checks, external
        connectivity tests are skipped to avoid network calls during tests.
        """
        # Initialize BaseExternalClient
        super().__init__(
            client_name="data_sanitizer",
            service_name="Presidio_Services",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=3,  # Lower threshold for privacy-critical service
            circuit_breaker_timeout=30,  # Shorter timeout for privacy service recovery
        )

        # Resolve settings — use injected settings or fall back to global
        if settings is None:
            from faultmaven.config.settings import get_settings

            settings = get_settings()
        self._settings = settings

        # Presidio service endpoints
        analyzer_url = settings.protection.presidio_analyzer_url
        anonymizer_url = settings.protection.presidio_anonymizer_url

        self.analyzer_url = analyzer_url
        self.anonymizer_url = anonymizer_url

        # HTTP client configuration
        self.request_timeout = 10.0  # seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "User-Agent": "FaultMaven-DataSanitizer/1.0",
            }
        )

        # Determine whether to skip external checks
        # Skip when: tests (skip_service_checks), or protection is disabled (local deployment)
        skip_checks = getattr(settings.server, "skip_service_checks", False)
        protection_disabled = (
            not settings.protection.protection_enabled
            and not settings.protection.sanitize_pii
        )

        # Test service connectivity only when protection is enabled
        if skip_checks or protection_disabled:
            self.analyzer_available = False
            self.anonymizer_available = False
            if skip_checks:
                self.logger.info(
                    "Skipping Presidio health checks (SKIP_SERVICE_CHECKS=True)"
                )
            else:
                self.logger.info(
                    "Skipping Presidio health checks (protection disabled)"
                )
        else:
            self.analyzer_available = self._test_service_health(self.analyzer_url)
            self.anonymizer_available = self._test_service_health(self.anonymizer_url)
            if self.analyzer_available and self.anonymizer_available:
                self.logger.info("✅ Connected to K8s Presidio services")
            else:
                self.logger.warning(
                    f"⚠️ Limited Presidio connectivity - Analyzer: {self.analyzer_available}, Anonymizer: {self.anonymizer_available}"
                )
                self.logger.info("📝 Falling back to regex-only sanitization")

        # Presidio detection config from settings
        self._presidio_entities = list(settings.protection.entities_to_protect)
        self._presidio_score_threshold = settings.protection.min_score_threshold

        # Pattern-to-replacement mapping (in priority order)
        # IMPORTANT: Order matters - more specific patterns should come first
        self.pattern_replacements = [
            # OpenAI patterns first (most specific) - flexible length for test compatibility
            (r"sk-[0-9a-zA-Z]{32,}", "[API_KEY_REDACTED]"),
            (r"pk-[0-9a-zA-Z]{32,}", "[API_KEY_REDACTED]"),
            # AWS Access Keys
            (r"AKIA[0-9A-Z]{16}", "[AWS_ACCESS_KEY_REDACTED]"),
            # Generic API key patterns
            (r"api[_-]?key[_-]?[0-9a-fA-F]{32,}", "[API_KEY_REDACTED]"),
            # Database URLs
            (r"(mongodb|postgresql|mysql)://[^@]+@[^/\s]+", "[DATABASE_URL_REDACTED]"),
            # JWT tokens
            (
                r"eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*",
                "[JWT_TOKEN_REDACTED]",
            ),
            # Private keys
            (
                r"-----BEGIN PRIVATE KEY-----[^-]+-----END PRIVATE KEY-----",
                "[PRIVATE_KEY_REDACTED]",
            ),
            (
                r"-----BEGIN RSA PRIVATE KEY-----[^-]+-----END RSA PRIVATE KEY-----",
                "[PRIVATE_KEY_REDACTED]",
            ),
            # Docker registry credentials
            (
                r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}:[a-zA-Z0-9._%+-]+",
                "[CREDENTIALS_REDACTED]",
            ),
            # Kubernetes secrets
            (r"k8s[_-]?secret[_-]?[0-9a-fA-F]{32,}", "[K8S_SECRET_REDACTED]"),
            # Password patterns
            (r"\bpassword[_-]?[=:]\s*[^\s\n]+", "[PASSWORD_REDACTED]"),
            (r"\bpasswd[_-]?[=:]\s*[^\s\n]+", "[PASSWORD_REDACTED]"),
            # IP addresses (internal ranges)
            (
                r"\b(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)\d{1,3}\.\d{1,3}\b",
                "[IP_ADDRESS_REDACTED]",
            ),
            # MAC addresses
            (r"([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})", "[MAC_ADDRESS_REDACTED]"),
            # AWS Secret Access Keys — CONTEXT-ANCHORED. An AWS secret is 40
            # base64 chars, indistinguishable from any hash/SHA/base64 blob, so
            # it MUST be gated on an adjacent `aws...secret...key =` context.
            # A bare `[0-9a-zA-Z/+]{40}` pattern here previously corrupted opaque
            # data (e.g. Gemini thought_signature base64) — see #654.
            (
                r"(?i)aws[_-]?secret[_-]?access[_-]?key['\"]?\s*[=:]\s*"
                r"['\"]?[0-9a-zA-Z/+]{40}",
                "[AWS_SECRET_KEY_REDACTED]",
            ),
        ]

        # Dict keys whose values are opaque model/provider artifacts (base64
        # reasoning signatures, provider round-trip metadata). They carry no
        # user data and MUST pass through verbatim — redacting them corrupts the
        # bytes and breaks the provider's decode on the next call (#654).
        self._opaque_artifact_keys = frozenset(
            {
                "provider_metadata",
                "assistant_parts",
                "thoughtsignature",
                "thought_signature",
            }
        )

    def sanitize(self, data: Any) -> Any:
        """
        ISanitizer interface implementation

        Sanitize sensitive information from data of various types.

        Args:
            data: Data to sanitize (can be string, dict, list, etc.)

        Returns:
            Sanitized data of the same type
        """
        if data is None:
            return data

        if isinstance(data, str):
            return self._sanitize_text(data)
        elif isinstance(data, dict):
            return self._sanitize_dict(data)
        elif isinstance(data, list):
            return self._sanitize_list(data)
        elif isinstance(data, (int, float, bool)):
            # Primitive types that don't contain sensitive data
            return data
        else:
            # For other types, convert to string, sanitize, and return as string
            return self._sanitize_text(str(data))

    async def asanitize(self, data: Any) -> Any:
        """Async boundary for :meth:`sanitize`.

        Runs the CPU-bound regex passes AND the blocking Presidio HTTP round-trip
        on a worker thread via ``asyncio.to_thread``, so the event loop is NEVER
        blocked. Any ``async`` caller MUST use this instead of ``sanitize()`` —
        a synchronous ``sanitize()`` on the loop stalls it and, on k8s, starves
        the liveness probe (the same class as inline embedding, #654).
        """
        if data is None:
            return data
        return await asyncio.to_thread(self.sanitize, data)

    def _sanitize_text(self, text: str) -> str:
        """
        Sanitize sensitive information from text using consistent pseudonymization.

        Each unique sensitive value gets a stable indexed placeholder (e.g.,
        ``<IP_ADDRESS_1>``, ``<IP_ADDRESS_2>``), so the same value always maps to
        the same placeholder within a single sanitize() call. This preserves
        analytical relationships (the LLM can distinguish entities) while
        protecting privacy.

        Args:
            text: Input text to sanitize

        Returns:
            Sanitized text with sensitive information replaced
        """
        if not text or not isinstance(text, str):
            return text

        sanitized_text = text

        # Entity registry: tracks unique values → indexed placeholder per type.
        # Shared across regex and Presidio to ensure consistent numbering.
        entity_registry: Dict[str, Dict[str, str]] = {}  # type → {value → placeholder}

        def _get_placeholder(entity_type: str, value: str) -> str:
            """Return a consistent hashed placeholder for a given entity value."""
            if entity_type not in entity_registry:
                entity_registry[entity_type] = {}
            type_map = entity_registry[entity_type]
            if value not in type_map:
                hash_val = hashlib.md5(value.encode("utf-8")).hexdigest()[:6]
                type_map[value] = f"<{entity_type}_{hash_val}>"
            return type_map[value]

        # Apply pattern replacements with consistent indexing
        for pattern_str, replacement_template in self.pattern_replacements:
            pattern = re.compile(pattern_str, re.IGNORECASE | re.DOTALL)
            # Derive entity type from the replacement template, e.g.
            # "[IP_ADDRESS_REDACTED]" → "IP_ADDRESS"
            entity_type = replacement_template.strip("[]").replace("_REDACTED", "")

            def _indexed_replacer(match: re.Match, _type: str = entity_type) -> str:
                return _get_placeholder(_type, match.group(0))

            sanitized_text = pattern.sub(_indexed_replacer, sanitized_text)

        # Apply K8s Presidio PII detection if available
        if self.analyzer_available and self.anonymizer_available:
            sanitized_text = self._apply_presidio(sanitized_text, entity_registry)

        return sanitized_text

    def sanitize_text_with_registry(
        self,
        text: str,
        entity_registry: Dict[str, Dict[str, str]],
    ) -> str:
        """Sanitize text using an externally-provided entity registry.

        Same detection logic as _sanitize_text() (regex patterns + Presidio)
        but uses the shared registry for cross-call placeholder consistency.
        The registry is mutated in place — new entries are added as PII is
        discovered.

        Args:
            text: Input text to sanitize.
            entity_registry: External registry mapping
                {entity_type: {original_value: placeholder}}. Will be
                mutated with new entries.

        Returns:
            Sanitized text with consistent placeholders.
        """
        if not text or not isinstance(text, str):
            return text

        sanitized_text = text

        def _get_placeholder(entity_type: str, value: str) -> str:
            if entity_type not in entity_registry:
                entity_registry[entity_type] = {}
            type_map = entity_registry[entity_type]
            if value not in type_map:
                hash_val = hashlib.md5(value.encode("utf-8")).hexdigest()[:6]
                type_map[value] = f"<{entity_type}_{hash_val}>"
            return type_map[value]

        for pattern_str, replacement_template in self.pattern_replacements:
            pattern = re.compile(pattern_str, re.IGNORECASE | re.DOTALL)
            entity_type = replacement_template.strip("[]").replace("_REDACTED", "")

            def _indexed_replacer(match: re.Match, _type: str = entity_type) -> str:
                return _get_placeholder(_type, match.group(0))

            sanitized_text = pattern.sub(_indexed_replacer, sanitized_text)

        if self.analyzer_available and self.anonymizer_available:
            sanitized_text = self._apply_presidio(sanitized_text, entity_registry)

        return sanitized_text

    def _sanitize_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Sanitize dictionary data recursively

        Args:
            data: Dictionary to sanitize

        Returns:
            Sanitized dictionary
        """
        # Sensitive key patterns - if key matches these, sanitize the value
        sensitive_key_patterns = [
            r"password",
            r"passwd",
            r"secret",
            r"token",
            r"key",
            r"api[_-]?key",
            r"access[_-]?key",
            r"secret[_-]?key",
            r"private[_-]?key",
            r"auth[_-]?token",
        ]

        sanitized = {}
        for key, value in data.items():
            # Opaque model/provider artifacts pass through verbatim — never walk
            # them (redacting reasoning signatures corrupts them, #654).
            if isinstance(key, str) and key.lower() in self._opaque_artifact_keys:
                sanitized[key] = value
                continue

            # Sanitize the key itself
            sanitized_key = self.sanitize(key)

            # Check if key indicates sensitive data
            key_is_sensitive = False
            key_lower = str(key).lower()
            for pattern in sensitive_key_patterns:
                if re.search(pattern, key_lower, re.IGNORECASE):
                    key_is_sensitive = True
                    break

            if key_is_sensitive and isinstance(value, str):
                # For sensitive keys, replace the value with appropriate redaction
                if "password" in key_lower or "passwd" in key_lower:
                    sanitized_value = "[PASSWORD_REDACTED]"
                elif "token" in key_lower:
                    sanitized_value = "[TOKEN_REDACTED]"
                elif "key" in key_lower:
                    sanitized_value = "[KEY_REDACTED]"
                elif "secret" in key_lower:
                    sanitized_value = "[SECRET_REDACTED]"
                else:
                    sanitized_value = "[SENSITIVE_VALUE_REDACTED]"
            else:
                # Normal sanitization for non-sensitive keys
                sanitized_value = self.sanitize(value)

            sanitized[sanitized_key] = sanitized_value
        return sanitized

    def _sanitize_list(self, data: List[Any]) -> List[Any]:
        """
        Sanitize list data recursively

        Args:
            data: List to sanitize

        Returns:
            Sanitized list
        """
        return [self.sanitize(item) for item in data]

    # Legacy _apply_pattern method - no longer used
    # Replaced with pattern_replacements approach for better priority control

    def _test_service_health(self, service_url: str) -> bool:
        """Test if a Presidio service is available with external call wrapping"""

        def health_check():
            health_url = f"{service_url}/health"
            response = self.session.get(health_url, timeout=5.0)
            return response.status_code == 200

        try:
            return self.call_external_sync(
                operation_name="health_check",
                call_func=health_check,
                retries=1,
                retry_delay=1.0,
            )
        except Exception as e:
            self.logger.debug(f"Health check failed for {service_url}: {e}")
            return False

    def _apply_presidio(
        self, text: str, entity_registry: Dict[str, Dict[str, str]] | None = None
    ) -> str:
        """Apply K8s Presidio PII detection with consistent pseudonymization.

        Uses the Presidio *analyzer* to detect entities, then performs our own
        indexed replacement (skipping the Presidio anonymizer) so that each
        unique entity value gets a distinct, stable placeholder.
        """
        if not (self.analyzer_available and self.anonymizer_available):
            self.logger.debug("Presidio services not available, skipping PII detection")
            return text

        if entity_registry is None:
            entity_registry = {}

        try:
            # Step 1: Analyze text for PII entities using K8s analyzer service
            def analyze_text():
                analyze_payload = {
                    "text": text,
                    "language": "en",
                    "entities": self._presidio_entities,
                    "score_threshold": self._presidio_score_threshold,
                }

                analyze_response = self.session.post(
                    f"{self.analyzer_url}/analyze",
                    json=analyze_payload,
                    timeout=self.request_timeout,
                )

                if analyze_response.status_code != 200:
                    raise RuntimeError(
                        f"Presidio analyzer failed with status {analyze_response.status_code}"
                    )

                return analyze_response.json()

            analyzer_results = self.call_external_sync(
                operation_name="analyze_pii",
                call_func=analyze_text,
                retries=1,
                retry_delay=1.0,
            )

            if not analyzer_results:
                return text

            # Step 2: Build indexed replacements from analyzer results.
            # Sort by start position descending so we can replace right-to-left
            # without invalidating earlier offsets.
            sorted_results = sorted(
                analyzer_results, key=lambda r: r["start"], reverse=True
            )

            result = text
            for entity in sorted_results:
                start = entity["start"]
                end = entity["end"]
                entity_type = entity["entity_type"]
                original_value = text[start:end]

                # Get or create hashed placeholder via shared registry
                if entity_type not in entity_registry:
                    entity_registry[entity_type] = {}
                type_map = entity_registry[entity_type]
                if original_value not in type_map:
                    hash_val = hashlib.md5(original_value.encode("utf-8")).hexdigest()[
                        :6
                    ]
                    type_map[original_value] = f"<{entity_type}_{hash_val}>"

                result = result[:start] + type_map[original_value] + result[end:]

            return result

        except Exception as e:
            self.logger.warning(f"Presidio K8s service error: {e}")
            if "Connection" in str(e) or "Timeout" in str(e):
                self.analyzer_available = False
                self.anonymizer_available = False
            return text

    def is_sensitive(self, data: Any) -> bool:
        """
        Check if data contains sensitive information

        Args:
            data: Data to check (string, dict, list, etc.)

        Returns:
            True if sensitive information is detected
        """
        if data is None:
            return False

        if isinstance(data, str):
            return self._is_text_sensitive(data)
        elif isinstance(data, dict):
            return any(
                self.is_sensitive(key) or self.is_sensitive(value)
                for key, value in data.items()
            )
        elif isinstance(data, list):
            return any(self.is_sensitive(item) for item in data)
        elif isinstance(data, (int, float, bool)):
            # Primitive types typically don't contain sensitive data
            return False
        else:
            # For other types, convert to string and check
            return self._is_text_sensitive(str(data))

    def _is_text_sensitive(self, text: str) -> bool:
        """
        Check if text contains sensitive information with external call wrapping

        Args:
            text: Text to check

        Returns:
            True if sensitive information is detected
        """
        if not text:
            return False

        # Check custom patterns
        for pattern_str, _ in self.pattern_replacements:
            pattern = re.compile(pattern_str, re.IGNORECASE | re.DOTALL)
            if pattern.search(text):
                return True

        # Check with K8s Presidio analyzer if available
        if self.analyzer_available:
            try:

                def check_sensitivity():
                    analyze_payload = {
                        "text": text,
                        "language": "en",
                        "entities": self._presidio_entities,
                        "score_threshold": self._presidio_score_threshold,
                    }

                    analyze_response = self.session.post(
                        f"{self.analyzer_url}/analyze",
                        json=analyze_payload,
                        timeout=self.request_timeout,
                    )

                    if analyze_response.status_code == 200:
                        analyzer_results = analyze_response.json()
                        return len(analyzer_results) > 0
                    return False

                return self.call_external_sync(
                    operation_name="check_sensitivity",
                    call_func=check_sensitivity,
                    retries=1,
                    retry_delay=0.5,
                )

            except Exception as e:
                self.logger.warning(f"Presidio K8s sensitivity check failed: {e}")

        return False

    async def health_check(self) -> Dict[str, Any]:
        """
        Perform comprehensive health check for DataSanitizer.

        Returns:
            Dictionary containing health status and metrics
        """
        base_health = await super().health_check()

        # Add sanitizer-specific health data
        try:
            # Test service connectivity
            analyzer_health = self._test_service_health(self.analyzer_url)
            anonymizer_health = self._test_service_health(self.anonymizer_url)

            sanitizer_health = {
                "analyzer_available": analyzer_health,
                "anonymizer_available": anonymizer_health,
                "presidio_services": {
                    "analyzer_url": self.analyzer_url,
                    "anonymizer_url": self.anonymizer_url,
                },
                "pattern_replacements_count": len(self.pattern_replacements),
            }

            # Determine overall status
            if analyzer_health and anonymizer_health:
                status = "healthy"
            elif analyzer_health or anonymizer_health:
                status = "degraded"  # Partial functionality
            else:
                status = "degraded"  # Custom patterns still work

            base_health.update(
                {"sanitizer_specific": sanitizer_health, "status": status}
            )

        except Exception as e:
            base_health.update(
                {"sanitizer_specific": {"error": str(e)}, "status": "unhealthy"}
            )

        return base_health
