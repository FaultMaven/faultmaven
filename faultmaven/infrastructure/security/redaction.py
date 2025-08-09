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

import logging
import os
import re
from typing import Any, Dict, List, Optional
import requests
import json
from faultmaven.models.interfaces import ISanitizer
from faultmaven.infrastructure.base_client import BaseExternalClient


class DataSanitizer(BaseExternalClient, ISanitizer):
    """Sanitizes sensitive information from text data
    
    Implements ISanitizer interface for privacy-first data processing.
    Supports both Presidio-based PII detection and custom regex patterns.
    """

    def __init__(self):
        """Initialize the DataSanitizer with K8s Presidio service and custom patterns"""
        # Initialize BaseExternalClient
        super().__init__(
            client_name="data_sanitizer",
            service_name="Presidio_Services",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=3,  # Lower threshold for privacy-critical service
            circuit_breaker_timeout=30    # Shorter timeout for privacy service recovery
        )

        # Configure K8s Presidio service endpoints (via NGINX Ingress)
        presidio_analyzer_host = os.getenv("PRESIDIO_ANALYZER_HOST", "presidio-analyzer.faultmaven.local")
        presidio_anonymizer_host = os.getenv("PRESIDIO_ANONYMIZER_HOST", "presidio-anonymizer.faultmaven.local")
        presidio_port = int(os.getenv("PRESIDIO_PORT", "30080"))
        
        self.analyzer_url = f"http://{presidio_analyzer_host}:{presidio_port}"
        self.anonymizer_url = f"http://{presidio_anonymizer_host}:{presidio_port}"
        
        # HTTP client configuration
        self.request_timeout = 10.0  # seconds
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'User-Agent': 'FaultMaven-DataSanitizer/1.0'
        })
        
        # Test service connectivity
        self.analyzer_available = self._test_service_health(self.analyzer_url)
        self.anonymizer_available = self._test_service_health(self.anonymizer_url)
        
        if self.analyzer_available and self.anonymizer_available:
            self.logger.info(f"✅ Connected to K8s Presidio services (Analyzer: {presidio_analyzer_host}, Anonymizer: {presidio_anonymizer_host})")
        else:
            self.logger.warning(f"⚠️ Limited Presidio connectivity - Analyzer: {self.analyzer_available}, Anonymizer: {self.anonymizer_available}")
            self.logger.info("📝 Falling back to regex-only sanitization")

        # Custom regex patterns for cloud secrets and sensitive data
        self.custom_patterns = [
            # AWS Access Keys
            re.compile(r"AKIA[0-9A-Z]{16}", re.IGNORECASE),
            # AWS Secret Access Keys
            re.compile(r"[0-9a-zA-Z/+]{40}", re.IGNORECASE),
            # API Keys (common patterns)
            re.compile(r"api[_-]?key[_-]?[0-9a-fA-F]{32,}", re.IGNORECASE),
            re.compile(r"sk-[0-9a-zA-Z]{48}", re.IGNORECASE),  # OpenAI keys
            re.compile(r"pk-[0-9a-zA-Z]{48}", re.IGNORECASE),  # OpenAI public keys
            # Database connection strings
            re.compile(r"(mongodb|postgresql|mysql)://[^@]+@[^/\s]+", re.IGNORECASE),
            # JWT tokens
            re.compile(
                r"eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*",
                re.IGNORECASE,
            ),
            # Private keys (PEM format)
            re.compile(
                r"-----BEGIN PRIVATE KEY-----[^-]+-----END PRIVATE KEY-----", re.DOTALL
            ),
            re.compile(
                r"-----BEGIN RSA PRIVATE KEY-----[^-]+-----END RSA PRIVATE KEY-----",
                re.DOTALL,
            ),
            # Docker registry credentials
            re.compile(
                r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}:[a-zA-Z0-9._%+-]+",
                re.IGNORECASE,
            ),
            # Kubernetes secrets
            re.compile(r"k8s[_-]?secret[_-]?[0-9a-fA-F]{32,}", re.IGNORECASE),
            # Generic password patterns
            re.compile(r"password[_-]?[=:]\s*[^\s\n]+", re.IGNORECASE),
            re.compile(r"passwd[_-]?[=:]\s*[^\s\n]+", re.IGNORECASE),
            # IP addresses (internal ranges)
            re.compile(
                r"\b(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)\d{1,3}\.\d{1,3}\b"
            ),
            # MAC addresses
            re.compile(r"([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})"),
        ]

        # Replacement patterns
        self.replacements = {
            "aws_access_key": "[AWS_ACCESS_KEY_REDACTED]",
            "aws_secret_key": "[AWS_SECRET_KEY_REDACTED]",
            "api_key": "[API_KEY_REDACTED]",
            "database_url": "[DATABASE_URL_REDACTED]",
            "jwt_token": "[JWT_TOKEN_REDACTED]",
            "private_key": "[PRIVATE_KEY_REDACTED]",
            "password": "[PASSWORD_REDACTED]",
            "ip_address": "[IP_ADDRESS_REDACTED]",
            "mac_address": "[MAC_ADDRESS_REDACTED]",
        }

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
    
    def _sanitize_text(self, text: str) -> str:
        """
        Sanitize sensitive information from text

        Args:
            text: Input text to sanitize

        Returns:
            Sanitized text with sensitive information replaced
        """
        if not text or not isinstance(text, str):
            return text

        sanitized_text = text

        # Apply custom regex patterns
        for pattern in self.custom_patterns:
            sanitized_text = self._apply_pattern(sanitized_text, pattern)

        # Apply K8s Presidio PII detection if available
        if self.analyzer_available and self.anonymizer_available:
            sanitized_text = self._apply_presidio(sanitized_text)

        return sanitized_text
    
    def _sanitize_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Sanitize dictionary data recursively
        
        Args:
            data: Dictionary to sanitize
            
        Returns:
            Sanitized dictionary
        """
        sanitized = {}
        for key, value in data.items():
            # Sanitize both keys and values
            sanitized_key = self.sanitize(key)
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

    def _apply_pattern(self, text: str, pattern: re.Pattern) -> str:
        """Apply a specific regex pattern to redact matches"""

        def replace_match(match):
            match_text = match.group(0)

            # Determine replacement based on pattern characteristics
            if "AKIA" in match_text.upper():
                return self.replacements["aws_access_key"]
            elif len(match_text) == 40 and match_text.isalnum():
                return self.replacements["aws_secret_key"]
            elif "api" in match_text.lower() and "key" in match_text.lower():
                return self.replacements["api_key"]
            elif any(
                db in match_text.lower()
                for db in ["mongodb://", "postgresql://", "mysql://"]
            ):
                return self.replacements["database_url"]
            elif match_text.startswith("eyJ"):
                return self.replacements["jwt_token"]
            elif "PRIVATE KEY" in match_text:
                return self.replacements["private_key"]
            elif "password" in match_text.lower():
                return self.replacements["password"]
            elif re.match(
                r"\b(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)\d{1,3}\.\d{1,3}\b",
                match_text,
            ):
                return self.replacements["ip_address"]
            elif re.match(r"([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})", match_text):
                return self.replacements["mac_address"]
            else:
                return "[SENSITIVE_DATA_REDACTED]"

        return pattern.sub(replace_match, text)

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
                retry_delay=1.0
            )
        except Exception as e:
            self.logger.debug(f"Health check failed for {service_url}: {e}")
            return False

    def _apply_presidio(self, text: str) -> str:
        """Apply K8s Presidio PII detection and redaction with external call wrapping"""
        if not (self.analyzer_available and self.anonymizer_available):
            self.logger.debug("Presidio services not available, skipping PII detection")
            return text
            
        try:
            # Step 1: Analyze text for PII entities using K8s analyzer service
            def analyze_text():
                analyze_payload = {
                    "text": text,
                    "language": "en"
                }
                
                analyze_response = self.session.post(
                    f"{self.analyzer_url}/analyze",
                    json=analyze_payload,
                    timeout=self.request_timeout
                )
                
                if analyze_response.status_code != 200:
                    raise RuntimeError(f"Presidio analyzer failed with status {analyze_response.status_code}")
                    
                return analyze_response.json()
            
            analyzer_results = self.call_external_sync(
                operation_name="analyze_pii",
                call_func=analyze_text,
                retries=1,
                retry_delay=1.0
            )
            
            if not analyzer_results:
                # No PII detected
                return text
            
            # Step 2: Anonymize text using K8s anonymizer service
            def anonymize_text():
                anonymize_payload = {
                    "text": text,
                    "analyzer_results": analyzer_results
                }
                
                anonymize_response = self.session.post(
                    f"{self.anonymizer_url}/anonymize", 
                    json=anonymize_payload,
                    timeout=self.request_timeout
                )
                
                if anonymize_response.status_code != 200:
                    raise RuntimeError(f"Presidio anonymizer failed with status {anonymize_response.status_code}")
                    
                return anonymize_response.json()
            
            anonymize_result = self.call_external_sync(
                operation_name="anonymize_pii",
                call_func=anonymize_text,
                retries=1,
                retry_delay=1.0
            )
            
            return anonymize_result.get("text", text)

        except Exception as e:
            self.logger.warning(f"Presidio K8s service error: {e}")
            # Mark services as unavailable for next requests on connection errors
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
            return any(self.is_sensitive(key) or self.is_sensitive(value) 
                      for key, value in data.items())
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
        for pattern in self.custom_patterns:
            if pattern.search(text):
                return True

        # Check with K8s Presidio analyzer if available
        if self.analyzer_available:
            try:
                def check_sensitivity():
                    analyze_payload = {
                        "text": text,
                        "language": "en"
                    }
                    
                    analyze_response = self.session.post(
                        f"{self.analyzer_url}/analyze",
                        json=analyze_payload,
                        timeout=self.request_timeout
                    )
                    
                    if analyze_response.status_code == 200:
                        analyzer_results = analyze_response.json()
                        return len(analyzer_results) > 0
                    return False
                
                return self.call_external_sync(
                    operation_name="check_sensitivity",
                    call_func=check_sensitivity,
                    retries=1,
                    retry_delay=0.5
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
                    "anonymizer_url": self.anonymizer_url
                },
                "custom_patterns_count": len(self.custom_patterns),
                "replacement_patterns_count": len(self.replacements)
            }
            
            # Determine overall status
            if analyzer_health and anonymizer_health:
                status = "healthy"
            elif analyzer_health or anonymizer_health:
                status = "degraded"  # Partial functionality
            else:
                status = "degraded"  # Custom patterns still work
            
            base_health.update({
                "sanitizer_specific": sanitizer_health,
                "status": status
            })
            
        except Exception as e:
            base_health.update({
                "sanitizer_specific": {"error": str(e)},
                "status": "unhealthy"
            })
        
        return base_health
