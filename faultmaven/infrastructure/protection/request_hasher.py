"""
Request hashing for deduplication

Provides secure, consistent hashing of requests for duplicate detection
with normalization and security features.
"""

import hashlib
import hmac
import json
import re
import time
from typing import Dict, Any, Optional, List, Set
from urllib.parse import parse_qs, urlencode
import logging

from ...models.protection import DeduplicationConfig


class RequestHasher:
    """
    Secure request hasher for deduplication
    
    Features:
    - Content normalization (removes timestamps, IDs, etc.)
    - Cryptographically secure hashing
    - Configurable sensitive field exclusion
    - Protection against hash collision attacks
    - Support for different content types
    """
    
    def __init__(self, salt: str = "faultmaven_dedup_salt"):
        self.salt = salt
        self.logger = logging.getLogger(__name__)
        
        # Fields to exclude from hashing (to normalize requests)
        self.excluded_fields: Set[str] = {
            # Timestamps
            "timestamp", "created_at", "updated_at", "request_time",
            "client_timestamp", "server_timestamp",
            
            # Request IDs
            "request_id", "correlation_id", "trace_id", "span_id",
            "transaction_id", "uuid", "guid",
            
            # Session/auth (included separately)
            "session_token", "auth_token", "access_token", "csrf_token",
            
            # Browser/client specific
            "user_agent", "browser_info", "client_version",
            "screen_resolution", "viewport_size",
            
            # Caching/optimization
            "cache_buster", "v", "version", "_", "t"
        }
        
        # Patterns to normalize in string values
        self.normalization_patterns = [
            # Timestamp patterns
            (r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', '[TIMESTAMP]'),
            (r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', '[TIMESTAMP]'),
            (r'\d{13}', '[EPOCH_MS]'),  # Epoch milliseconds
            (r'\d{10}', '[EPOCH_S]'),   # Epoch seconds
            
            # UUID patterns
            (r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '[UUID]'),
            (r'[0-9a-f]{32}', '[HASH32]'),  # 32-char hex
            
            # Request ID patterns
            (r'req_[a-zA-Z0-9]+', '[REQUEST_ID]'),
            (r'trace_[a-zA-Z0-9]+', '[TRACE_ID]'),
            
            # File paths with timestamps
            (r'/tmp/[^/\s]+', '[TEMP_PATH]'),
            (r'/var/log/[^/\s]+', '[LOG_PATH]'),
        ]
        
        # Compiled regex patterns for performance
        self._compiled_patterns = [
            (re.compile(pattern, re.IGNORECASE), replacement)
            for pattern, replacement in self.normalization_patterns
        ]
    
    def hash_request(
        self,
        session_id: str,
        endpoint: str,
        method: str = "POST",
        body: Optional[str] = None,
        query_params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None
    ) -> str:
        """
        Generate a secure hash for request deduplication
        
        Args:
            session_id: Session identifier
            endpoint: API endpoint path
            method: HTTP method
            body: Request body (JSON string or raw)
            query_params: Query parameters
            headers: Request headers
            
        Returns:
            Hex-encoded SHA-256 hash
        """
        try:
            # Normalize all components
            normalized_endpoint = self._normalize_endpoint(endpoint)
            normalized_body = self._normalize_body(body)
            normalized_params = self._normalize_params(query_params)
            normalized_headers = self._normalize_headers(headers)
            
            # Create hash input
            hash_components = [
                session_id,
                method.upper(),
                normalized_endpoint,
                normalized_body,
                normalized_params,
                normalized_headers
            ]
            
            # Join components with delimiter
            content = "|".join(str(comp) for comp in hash_components)
            
            # Generate secure hash with salt
            return self._secure_hash(content)
            
        except Exception as e:
            self.logger.error(f"Request hashing failed: {e}")
            # Fallback: simple hash without normalization
            fallback_content = f"{session_id}:{endpoint}:{method}"
            return hashlib.sha256(fallback_content.encode()).hexdigest()
    
    def _normalize_endpoint(self, endpoint: str) -> str:
        """Normalize endpoint path"""
        if not endpoint:
            return ""
        
        # Remove query parameters from endpoint
        endpoint = endpoint.split('?')[0]
        
        # Normalize path separators
        endpoint = endpoint.replace('\\', '/')
        
        # Remove trailing slashes
        endpoint = endpoint.rstrip('/')
        
        # Convert to lowercase for consistency
        endpoint = endpoint.lower()
        
        return endpoint
    
    def _normalize_body(self, body: Optional[str]) -> str:
        """Normalize request body content"""
        if not body:
            return ""
        
        try:
            # Try to parse as JSON for proper normalization
            if body.strip().startswith(('{', '[')):
                parsed = json.loads(body)
                normalized = self._normalize_json_object(parsed)
                return json.dumps(normalized, sort_keys=True, separators=(',', ':'))
            else:
                # Plain text normalization
                return self._normalize_text(body)
                
        except json.JSONDecodeError:
            # Not JSON, treat as plain text
            return self._normalize_text(body)
        except Exception as e:
            self.logger.warning(f"Body normalization failed: {e}")
            return self._normalize_text(str(body))
    
    def _normalize_json_object(self, obj: Any) -> Any:
        """Recursively normalize JSON object"""
        if isinstance(obj, dict):
            normalized = {}
            for key, value in obj.items():
                # Skip excluded fields
                if key.lower() in self.excluded_fields:
                    continue
                
                # Recursively normalize value
                normalized[key] = self._normalize_json_object(value)
            
            return normalized
            
        elif isinstance(obj, list):
            return [self._normalize_json_object(item) for item in obj]
            
        elif isinstance(obj, str):
            return self._normalize_text(obj)
            
        else:
            # Numbers, booleans, null - return as-is
            return obj
    
    def _normalize_text(self, text: str) -> str:
        """Normalize text content"""
        if not text:
            return ""
        
        # Apply normalization patterns
        normalized = text
        for pattern, replacement in self._compiled_patterns:
            normalized = pattern.sub(replacement, normalized)
        
        # Normalize whitespace
        normalized = re.sub(r'\s+', ' ', normalized.strip())
        
        return normalized
    
    def _normalize_params(self, params: Optional[Dict[str, Any]]) -> str:
        """Normalize query parameters"""
        if not params:
            return ""
        
        # Filter out excluded parameters
        filtered_params = {
            k: v for k, v in params.items()
            if k.lower() not in self.excluded_fields
        }
        
        # Sort parameters for consistency
        sorted_params = sorted(filtered_params.items())
        
        # Normalize values
        normalized_params = []
        for key, value in sorted_params:
            if isinstance(value, str):
                value = self._normalize_text(value)
            elif isinstance(value, list):
                value = [self._normalize_text(str(v)) for v in value]
                value.sort()  # Sort list values
            
            normalized_params.append((key, value))
        
        # Convert to string representation
        return json.dumps(normalized_params, sort_keys=True, separators=(',', ':'))
    
    def _normalize_headers(self, headers: Optional[Dict[str, str]]) -> str:
        """Normalize relevant headers"""
        if not headers:
            return ""
        
        # Only include specific headers that affect processing
        relevant_headers = {
            "content-type", "accept", "accept-language",
            "accept-encoding"  # But exclude others that vary per request
        }
        
        normalized_headers = {}
        for key, value in headers.items():
            key_lower = key.lower()
            if key_lower in relevant_headers:
                normalized_headers[key_lower] = value.lower().strip()
        
        # Sort for consistency
        sorted_headers = sorted(normalized_headers.items())
        return json.dumps(sorted_headers, separators=(',', ':'))
    
    def _secure_hash(self, content: str) -> str:
        """Generate cryptographically secure hash"""
        # Use PBKDF2 for additional security against rainbow table attacks
        return hashlib.pbkdf2_hmac(
            'sha256',
            content.encode('utf-8'),
            self.salt.encode('utf-8'),
            100000  # iterations
        ).hex()
    
    def hash_title_generation_request(
        self,
        session_id: str,
        conversation_context: Optional[str] = None
    ) -> str:
        """
        Specialized hash for title generation requests
        
        Title generation is particularly vulnerable to infinite loops,
        so we use a very specific hash that ignores minor variations.
        """
        try:
            # For title generation, we only care about:
            # 1. Session ID (who is asking)
            # 2. Basic conversation existence (not specific content)
            
            # Normalize conversation context to just presence/absence
            has_conversation = "yes" if conversation_context and conversation_context.strip() else "no"
            
            # Create a simple, stable hash
            content = f"title_generation:{session_id}:{has_conversation}"
            
            return self._secure_hash(content)
            
        except Exception as e:
            self.logger.error(f"Title generation hash failed: {e}")
            return self._secure_hash(f"title_generation:{session_id}")
    
    def validate_hash(self, hash_value: str) -> bool:
        """Validate that a hash has the expected format"""
        if not hash_value:
            return False
        
        # Should be hex string of specific length (PBKDF2 SHA-256 output)
        if len(hash_value) != 64:  # 32 bytes * 2 hex chars
            return False
        
        # Should only contain hex characters
        try:
            int(hash_value, 16)
            return True
        except ValueError:
            return False
    
    def get_hash_stats(self) -> Dict[str, Any]:
        """Get statistics about hashing operations"""
        return {
            "excluded_fields_count": len(self.excluded_fields),
            "normalization_patterns_count": len(self.normalization_patterns),
            "hash_algorithm": "PBKDF2-SHA256",
            "iterations": 100000,
            "output_length": 64  # hex characters
        }