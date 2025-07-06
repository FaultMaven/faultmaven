"""redaction.py

Purpose: Sanitize sensitive information

Requirements:
--------------------------------------------------------------------------------
• Implement DataSanitizer class
• Use Microsoft Presidio for PII detection
• Apply custom regex patterns for secrets

Key Components:
--------------------------------------------------------------------------------
  class DataSanitizer: sanitize(text: str) -> str
  CUSTOM_PATTERNS: List[re.Pattern]

Technology Stack:
--------------------------------------------------------------------------------
presidio-analyzer, regex

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Cost-Efficiency: Use semantic caching
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import re
import logging
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.analyzer_request import AnalyzerRequest


class DataSanitizer:
    """Sanitizes sensitive information from text data"""
    
    def __init__(self):
        """Initialize the DataSanitizer with Presidio and custom patterns"""
        self.logger = logging.getLogger(__name__)
        
        # Initialize Presidio analyzer
        try:
            provider = NlpEngineProvider(conf_file=None)
            nlp_engine = provider.create_engine()
            self.analyzer = AnalyzerEngine(nlp_engine=nlp_engine)
        except Exception as e:
            self.logger.warning(f"Failed to initialize Presidio: {e}")
            self.analyzer = None
        
        # Custom regex patterns for cloud secrets and sensitive data
        self.custom_patterns = [
            # AWS Access Keys
            re.compile(r'AKIA[0-9A-Z]{16}', re.IGNORECASE),
            # AWS Secret Access Keys
            re.compile(r'[0-9a-zA-Z/+]{40}', re.IGNORECASE),
            # API Keys (common patterns)
            re.compile(
                r'api[_-]?key[_-]?[0-9a-fA-F]{32,}', re.IGNORECASE
            ),
            re.compile(r'sk-[0-9a-zA-Z]{48}', re.IGNORECASE),  # OpenAI keys
            re.compile(r'pk-[0-9a-zA-Z]{48}', re.IGNORECASE),  # OpenAI public keys
            # Database connection strings
            re.compile(
                r'(mongodb|postgresql|mysql)://[^@]+@[^/\s]+', re.IGNORECASE
            ),
            # JWT tokens
            re.compile(
                r'eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*',
                re.IGNORECASE
            ),
            # Private keys (PEM format)
            re.compile(
                r'-----BEGIN PRIVATE KEY-----[^-]+-----END PRIVATE KEY-----',
                re.DOTALL
            ),
            re.compile(
                r'-----BEGIN RSA PRIVATE KEY-----[^-]+-----END RSA PRIVATE KEY-----',
                re.DOTALL
            ),
            # Docker registry credentials
            re.compile(
                r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}:[a-zA-Z0-9._%+-]+',
                re.IGNORECASE
            ),
            # Kubernetes secrets
            re.compile(
                r'k8s[_-]?secret[_-]?[0-9a-fA-F]{32,}', re.IGNORECASE
            ),
            # Generic password patterns
            re.compile(r'password[_-]?[=:]\s*[^\s\n]+', re.IGNORECASE),
            re.compile(r'passwd[_-]?[=:]\s*[^\s\n]+', re.IGNORECASE),
            # IP addresses (internal ranges)
            re.compile(
                r'\b(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)\d{1,3}\.\d{1,3}\b'
            ),
            # MAC addresses
            re.compile(r'([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})'),
        ]
        
        # Replacement patterns
        self.replacements = {
            'aws_access_key': '[AWS_ACCESS_KEY_REDACTED]',
            'aws_secret_key': '[AWS_SECRET_KEY_REDACTED]',
            'api_key': '[API_KEY_REDACTED]',
            'database_url': '[DATABASE_URL_REDACTED]',
            'jwt_token': '[JWT_TOKEN_REDACTED]',
            'private_key': '[PRIVATE_KEY_REDACTED]',
            'password': '[PASSWORD_REDACTED]',
            'ip_address': '[IP_ADDRESS_REDACTED]',
            'mac_address': '[MAC_ADDRESS_REDACTED]',
        }
    
    def sanitize(self, text: str) -> str:
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
        
        # Apply Presidio PII detection if available
        if self.analyzer:
            sanitized_text = self._apply_presidio(sanitized_text)
        
        return sanitized_text
    
    def _apply_pattern(self, text: str, pattern: re.Pattern) -> str:
        """Apply a specific regex pattern to redact matches"""
        def replace_match(match):
            match_text = match.group(0)
            
            # Determine replacement based on pattern characteristics
            if 'AKIA' in match_text.upper():
                return self.replacements['aws_access_key']
            elif len(match_text) == 40 and match_text.isalnum():
                return self.replacements['aws_secret_key']
            elif 'api' in match_text.lower() and 'key' in match_text.lower():
                return self.replacements['api_key']
            elif any(db in match_text.lower() for db in ['mongodb://', 'postgresql://', 'mysql://']):
                return self.replacements['database_url']
            elif match_text.startswith('eyJ'):
                return self.replacements['jwt_token']
            elif 'PRIVATE KEY' in match_text:
                return self.replacements['private_key']
            elif 'password' in match_text.lower():
                return self.replacements['password']
            elif re.match(r'\b(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)\d{1,3}\.\d{1,3}\b', match_text):
                return self.replacements['ip_address']
            elif re.match(r'([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})', match_text):
                return self.replacements['mac_address']
            else:
                return '[SENSITIVE_DATA_REDACTED]'
        
        return pattern.sub(replace_match, text)
    
    def _apply_presidio(self, text: str) -> str:
        """Apply Presidio PII detection and redaction"""
        try:
            # Create analyzer request
            analyzer_request = AnalyzerRequest(
                text=text,
                language="en"
            )
            
            # Get PII entities
            results = self.analyzer.analyze(analyzer_request)
            
            # Sort results by start position (descending) to avoid index shifting
            results = sorted(results, key=lambda x: x.start, reverse=True)
            
            # Replace detected entities
            sanitized_text = text
            for result in results:
                start = result.start
                end = result.end
                entity_type = result.entity_type
                
                # Create appropriate replacement based on entity type
                replacement = f'[{entity_type.upper()}_REDACTED]'
                sanitized_text = sanitized_text[:start] + replacement + sanitized_text[end:]
            
            return sanitized_text
            
        except Exception as e:
            self.logger.warning(f"Presidio analysis failed: {e}")
            return text
    
    def is_sensitive(self, text: str) -> bool:
        """
        Check if text contains sensitive information
        
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
        
        # Check with Presidio if available
        if self.analyzer:
            try:
                analyzer_request = AnalyzerRequest(text=text, language="en")
                results = self.analyzer.analyze(analyzer_request)
                return len(results) > 0
            except Exception as e:
                self.logger.warning(f"Presidio sensitivity check failed: {e}")
        
        return False

