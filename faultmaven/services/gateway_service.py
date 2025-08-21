"""Gateway Processing Service - Phase B Implementation

This module implements the IGatewayProcessingService interface from the microservice
architecture blueprint, providing pre-processing and filtering for all incoming queries
before they reach the agent orchestration layer.

Key Features:
- Clarity assessment for query specificity and actionability  
- Reality filter for basic sanity checking and feasibility assessment
- Assumption extractor to identify implicit assumptions in queries
- PII redaction integration with existing DataSanitizer
- Input validation with comprehensive safety filters
- Performance optimized (p95 < 100ms)

Implementation Notes:
- Uses existing DataSanitizer for PII redaction
- Integrates with LLM provider for semantic analysis
- Comprehensive error handling with fallback strategies
- Thread-safe operations with async patterns
- Built-in observability and metrics tracking
"""

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from faultmaven.services.microservice_interfaces.core_services import IGatewayProcessingService
from faultmaven.models.microservice_contracts.core_contracts import GatewayResult
from faultmaven.models.interfaces import ILLMProvider, ISanitizer, ITracer
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ValidationException, ServiceException


class GatewayProcessingService(IGatewayProcessingService):
    """
    Implementation of IGatewayProcessingService interface
    
    This service provides comprehensive pre-processing and filtering for incoming
    queries including clarity assessment, reality filtering, assumption extraction,
    and PII redaction before queries reach the orchestration layer.
    """

    # Query clarity assessment patterns
    CLARITY_PATTERNS = {
        'specific_error': [
            r'\b(error|exception|failed|timeout|connection|refused)\b',
            r'\b\d{3,4}\s*(error|status|code)\b',
            r'\b(stack\s*trace|traceback|backtrace)\b'
        ],
        'system_context': [
            r'\b(server|database|application|service|API)\b',
            r'\b(version|OS|environment|config|setup)\b',
            r'\b(production|staging|development|local)\b'
        ],
        'vague_indicators': [
            r'\b(something|somehow|somewhere|sometimes)\b',
            r'\b(doesn\'?t work|not working|broken|issues?)\b',
            r'\b(weird|strange|odd|random)\b'
        ],
        'urgency_indicators': [
            r'\b(urgent|critical|emergency|asap|immediately)\b',
            r'\b(down|outage|offline|crashed)\b',
            r'\b(customers?|users?|production)\s+(\w+\s+)?(affected|impacted)\b'
        ]
    }

    # Reality check patterns - obvious non-technical queries
    REALITY_FILTER_PATTERNS = {
        'non_technical': [
            r'\b(recipe|cooking|food|restaurant)\b',
            r'\b(weather|forecast|temperature)\b',
            r'\b(movie|film|entertainment|music)\b',
            r'\b(dating|relationship|personal)\b'
        ],
        'obvious_non_issues': [
            r'\b(hello|hi|hey|good morning|good afternoon)\b',
            r'\b(test|testing|check|just checking)\b',
            r'\b(thanks?|thank you|thx)\b'
        ],
        'malicious_indicators': [
            r'<script[^>]*>.*?</script>',
            r'\b(drop|delete|truncate)\s+(table|database|schema)\b',
            r'\b(union|select|insert|update)\s+.*\b(from|into|set)\b',
            r'\b(exec|execute|eval|system|cmd)\s*\(',
            r'[\'"][^\'\"]*[\'\"]\s*;\s*--'
        ]
    }

    # Input validation limits
    VALIDATION_LIMITS = {
        'min_query_length': 3,
        'max_query_length': 10000,
        'max_url_count': 5,
        'max_special_char_ratio': 0.4,
        'max_repeated_char_ratio': 0.2
    }

    def __init__(
        self,
        llm_provider: ILLMProvider,
        sanitizer: ISanitizer,
        tracer: Optional[ITracer] = None,
        clarity_threshold: float = 0.6,
        reality_threshold: float = 0.8,
        enable_assumption_extraction: bool = True
    ):
        """
        Initialize Gateway Processing Service
        
        Args:
            llm_provider: LLM provider for semantic analysis
            sanitizer: Data sanitizer for PII redaction
            tracer: Optional tracer for observability
            clarity_threshold: Minimum clarity score to pass (0.0-1.0)
            reality_threshold: Minimum reality score to pass (0.0-1.0)
            enable_assumption_extraction: Whether to extract assumptions
        """
        self._llm_provider = llm_provider
        self._sanitizer = sanitizer
        self._tracer = tracer
        self._clarity_threshold = clarity_threshold
        self._reality_threshold = reality_threshold
        self._enable_assumption_extraction = enable_assumption_extraction
        self._logger = logging.getLogger(self.__class__.__name__)
        
        # Performance metrics
        self._metrics = {
            'queries_processed': 0,
            'queries_rejected': 0,
            'pii_redactions': 0,
            'avg_processing_time_ms': 0.0,
            'clarity_failures': 0,
            'reality_failures': 0,
            'validation_failures': 0
        }
        
        # Compile regex patterns for performance
        self._compiled_patterns = {}
        for category, patterns in {**self.CLARITY_PATTERNS, **self.REALITY_FILTER_PATTERNS}.items():
            self._compiled_patterns[category] = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]

    @trace("gateway_process_query")
    async def process_query(self, query: str, context: Optional[Dict[str, Any]] = None) -> GatewayResult:
        """
        Process query through complete gateway pipeline
        
        Args:
            query: Raw user query
            context: Optional context (session, user info, etc.)
            
        Returns:
            GatewayResult with processed query and assessment results
            
        Raises:
            ValidationException: When query fails validation
            ServiceException: When processing fails
        """
        start_time = time.time()
        
        try:
            # Step 1: Input validation
            validation_result = await self._validate_input(query, context or {})
            if not validation_result['valid']:
                self._metrics['validation_failures'] += 1
                self._metrics['queries_rejected'] += 1
                return GatewayResult(
                    original_query=query,
                    processed_query=query,
                    clarity_score=0.0,
                    reality_score=0.0,
                    assumptions=[],
                    pii_redacted=False,
                    passed_filters=False,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                    metadata={
                        'rejection_reason': validation_result['reason'],
                        'validation_errors': validation_result.get('errors', [])
                    }
                )
            
            # Step 2: PII redaction
            sanitized_query = await self._sanitize_query(query)
            pii_redacted = sanitized_query != query
            if pii_redacted:
                self._metrics['pii_redactions'] += 1
            
            # Step 3: Clarity check
            clarity_score = await self._assess_clarity(sanitized_query, context or {})
            
            # Step 4: Reality filter
            reality_score = await self._assess_reality(sanitized_query)
            
            # Step 5: Assumption extraction (if enabled)
            assumptions = []
            if self._enable_assumption_extraction:
                assumptions = await self._extract_assumptions(sanitized_query, context or {})
            
            # Determine if query passes all filters
            passed_filters = (
                clarity_score >= self._clarity_threshold and
                reality_score >= self._reality_threshold
            )
            
            if not passed_filters:
                self._metrics['queries_rejected'] += 1
                if clarity_score < self._clarity_threshold:
                    self._metrics['clarity_failures'] += 1
                if reality_score < self._reality_threshold:
                    self._metrics['reality_failures'] += 1
            
            processing_time_ms = int((time.time() - start_time) * 1000)
            self._metrics['queries_processed'] += 1
            self._update_avg_processing_time(processing_time_ms)
            
            result = GatewayResult(
                original_query=query,
                processed_query=sanitized_query,
                clarity_score=clarity_score,
                reality_score=reality_score,
                assumptions=assumptions,
                pii_redacted=pii_redacted,
                passed_filters=passed_filters,
                processing_time_ms=processing_time_ms,
                metadata={
                    'validation_passed': True,
                    'processing_steps': ['validation', 'sanitization', 'clarity', 'reality'],
                    'assumptions_extracted': len(assumptions),
                    'clarity_threshold': self._clarity_threshold,
                    'reality_threshold': self._reality_threshold
                }
            )
            
            self._logger.debug(
                f"Gateway processed query: clarity={clarity_score:.3f}, "
                f"reality={reality_score:.3f}, passed={passed_filters}, time={processing_time_ms}ms"
            )
            
            return result
            
        except Exception as e:
            processing_time_ms = int((time.time() - start_time) * 1000)
            self._logger.error(f"Gateway processing failed: {e}")
            self._metrics['queries_rejected'] += 1
            
            # Return error result instead of raising exception
            return GatewayResult(
                original_query=query,
                processed_query=query,
                clarity_score=0.0,
                reality_score=0.0,
                assumptions=[],
                pii_redacted=False,
                passed_filters=False,
                processing_time_ms=processing_time_ms,
                metadata={
                    'error': str(e),
                    'processing_failed': True
                }
            )

    async def _validate_input(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate input query for basic safety and format requirements
        
        Args:
            query: Raw query string
            context: Request context
            
        Returns:
            Dict with validation result and reason if invalid
        """
        if not query or not query.strip():
            return {
                'valid': False,
                'reason': 'empty_query',
                'errors': ['Query cannot be empty']
            }
        
        query_stripped = query.strip()
        
        # Length validation
        if len(query_stripped) < self.VALIDATION_LIMITS['min_query_length']:
            return {
                'valid': False,
                'reason': 'query_too_short',
                'errors': [f'Query must be at least {self.VALIDATION_LIMITS["min_query_length"]} characters']
            }
        
        if len(query_stripped) > self.VALIDATION_LIMITS['max_query_length']:
            return {
                'valid': False,
                'reason': 'query_too_long',
                'errors': [f'Query must be less than {self.VALIDATION_LIMITS["max_query_length"]} characters']
            }
        
        # Character composition validation
        special_chars = sum(1 for c in query_stripped if not c.isalnum() and not c.isspace())
        special_char_ratio = special_chars / len(query_stripped)
        
        if special_char_ratio > self.VALIDATION_LIMITS['max_special_char_ratio']:
            return {
                'valid': False,
                'reason': 'too_many_special_chars',
                'errors': ['Query contains too many special characters']
            }
        
        # Repeated character validation
        max_repeated = max((len(list(g)) for k, g in __import__('itertools').groupby(query_stripped)), default=0)
        repeated_ratio = max_repeated / len(query_stripped)
        
        if repeated_ratio > self.VALIDATION_LIMITS['max_repeated_char_ratio']:
            return {
                'valid': False,
                'reason': 'repeated_characters',
                'errors': ['Query contains too many repeated characters']
            }
        
        # URL validation
        urls = re.findall(r'https?://[^\s]+', query_stripped)
        if len(urls) > self.VALIDATION_LIMITS['max_url_count']:
            return {
                'valid': False,
                'reason': 'too_many_urls',
                'errors': [f'Query contains too many URLs (max {self.VALIDATION_LIMITS["max_url_count"]})']
            }
        
        # Basic malicious pattern detection
        malicious_patterns = self._compiled_patterns.get('malicious_indicators', [])
        for pattern in malicious_patterns:
            if pattern.search(query_stripped):
                return {
                    'valid': False,
                    'reason': 'malicious_content',
                    'errors': ['Query contains potentially malicious content']
                }
        
        return {'valid': True, 'reason': 'passed_validation'}

    async def _sanitize_query(self, query: str) -> str:
        """
        Sanitize query using existing DataSanitizer to remove PII
        
        Args:
            query: Raw query string
            
        Returns:
            Sanitized query with PII redacted
        """
        try:
            sanitized = self._sanitizer.sanitize(query)
            # sanitizer.sanitize() returns the same type as input
            return sanitized if isinstance(sanitized, str) else str(sanitized)
        except Exception as e:
            self._logger.warning(f"PII sanitization failed: {e}")
            # Return original query if sanitization fails
            return query

    async def _assess_clarity(self, query: str, context: Dict[str, Any]) -> float:
        """
        Assess query clarity based on specificity and actionability
        
        Args:
            query: Sanitized query
            context: Request context
            
        Returns:
            Clarity score (0.0-1.0)
        """
        try:
            clarity_score = 0.0
            query_lower = query.lower()
            
            # Pattern-based scoring
            specific_error_matches = sum(
                1 for pattern in self._compiled_patterns.get('specific_error', [])
                if pattern.search(query)
            )
            system_context_matches = sum(
                1 for pattern in self._compiled_patterns.get('system_context', [])
                if pattern.search(query)
            )
            vague_matches = sum(
                1 for pattern in self._compiled_patterns.get('vague_indicators', [])
                if pattern.search(query)
            )
            urgency_matches = sum(
                1 for pattern in self._compiled_patterns.get('urgency_indicators', [])
                if pattern.search(query)
            )
            
            # Base clarity score from specificity
            if specific_error_matches > 0:
                clarity_score += 0.4  # Specific errors are good indicators
            
            if system_context_matches > 0:
                clarity_score += 0.3  # System context adds clarity
                
            if urgency_matches > 0:
                clarity_score += 0.1  # Urgency indicators help prioritization
            
            # Penalize vague language
            clarity_score -= min(vague_matches * 0.2, 0.4)
            
            # Length-based adjustments
            word_count = len(query.split())
            if word_count < 5:
                clarity_score -= 0.2  # Too short to be clear
            elif word_count > 100:
                clarity_score -= 0.1  # Very long queries may lack focus
            elif 10 <= word_count <= 50:
                clarity_score += 0.1  # Good length for clarity
            
            # Question structure bonus
            if any(q_word in query_lower for q_word in ['how', 'why', 'what', 'when', 'where', 'which']):
                clarity_score += 0.1
            
            # Ensure score is in valid range
            return max(0.0, min(1.0, clarity_score))
            
        except Exception as e:
            self._logger.warning(f"Clarity assessment failed: {e}")
            return 0.5  # Default neutral score

    async def _assess_reality(self, query: str) -> float:
        """
        Assess query reality - filter out obviously non-technical queries
        
        Args:
            query: Sanitized query
            
        Returns:
            Reality score (0.0-1.0)
        """
        try:
            reality_score = 1.0  # Start with assumption it's valid
            
            # Check for non-technical patterns
            non_technical_matches = sum(
                1 for pattern in self._compiled_patterns.get('non_technical', [])
                if pattern.search(query)
            )
            
            if non_technical_matches > 0:
                reality_score -= 0.8  # Heavily penalize non-technical queries
            
            # Check for obvious non-issues
            non_issue_matches = sum(
                1 for pattern in self._compiled_patterns.get('obvious_non_issues', [])
                if pattern.search(query)
            )
            
            if non_issue_matches > 0:
                reality_score -= 0.6  # Penalize casual greetings and tests
            
            # Bonus for technical indicators
            tech_indicators = [
                'server', 'database', 'api', 'service', 'application', 'network',
                'error', 'exception', 'timeout', 'connection', 'config', 'log',
                'debug', 'deploy', 'version', 'port', 'ssl', 'certificate'
            ]
            
            tech_matches = sum(1 for indicator in tech_indicators if indicator in query.lower())
            if tech_matches > 0:
                reality_score += min(tech_matches * 0.05, 0.2)  # Bonus for technical terms
            
            # Ensure score is in valid range
            return max(0.0, min(1.0, reality_score))
            
        except Exception as e:
            self._logger.warning(f"Reality assessment failed: {e}")
            return 0.8  # Default high score - assume valid

    async def _extract_assumptions(self, query: str, context: Dict[str, Any]) -> List[str]:
        """
        Extract implicit assumptions from the query using LLM analysis
        
        Args:
            query: Sanitized query
            context: Request context
            
        Returns:
            List of identified assumptions
        """
        if not self._enable_assumption_extraction:
            return []
        
        try:
            prompt = f"""Analyze the following troubleshooting query and identify any implicit assumptions the user might be making. List each assumption on a separate line, keeping them concise and specific.

Query: "{query}"

Identify assumptions about:
- System state or configuration
- Expected behavior
- Problem scope or cause
- User environment or setup
- Previous troubleshooting attempts

Return only the assumptions, one per line, without explanations:"""

            response = await self._llm_provider.generate(
                prompt=prompt,
                max_tokens=300,
                temperature=0.1
            )
            
            if not response or not response.strip():
                return []
            
            # Parse assumptions from response
            assumptions = [
                line.strip().lstrip('-•*').strip()
                for line in response.strip().split('\n')
                if line.strip() and len(line.strip()) > 10
            ]
            
            # Filter and validate assumptions
            filtered_assumptions = []
            for assumption in assumptions[:10]:  # Limit to 10 assumptions
                if (len(assumption) >= 10 and 
                    len(assumption) <= 200 and
                    not assumption.lower().startswith(('query:', 'assumptions:', 'the user'))):
                    filtered_assumptions.append(assumption)
            
            return filtered_assumptions
            
        except Exception as e:
            self._logger.warning(f"Assumption extraction failed: {e}")
            return []

    def _update_avg_processing_time(self, processing_time_ms: int) -> None:
        """Update average processing time metric"""
        total_queries = self._metrics['queries_processed']
        if total_queries == 1:
            self._metrics['avg_processing_time_ms'] = processing_time_ms
        else:
            # Running average
            current_avg = self._metrics['avg_processing_time_ms']
            self._metrics['avg_processing_time_ms'] = (
                (current_avg * (total_queries - 1) + processing_time_ms) / total_queries
            )

    @trace("gateway_health_check")
    async def health_check(self) -> Dict[str, Any]:
        """
        Get service health status and performance metrics
        
        Returns:
            Health status including service metrics and dependency status
        """
        try:
            # Check dependency health
            dependencies = {
                'llm_provider': self._llm_provider is not None,
                'sanitizer': self._sanitizer is not None,
                'tracer': self._tracer is not None
            }
            
            # Calculate performance indicators
            total_processed = self._metrics['queries_processed']
            total_rejected = self._metrics['queries_rejected']
            
            if total_processed > 0:
                rejection_rate = total_rejected / (total_processed + total_rejected)
                clarity_failure_rate = self._metrics['clarity_failures'] / total_processed
                reality_failure_rate = self._metrics['reality_failures'] / total_processed
            else:
                rejection_rate = clarity_failure_rate = reality_failure_rate = 0.0
            
            # Determine service status
            service_status = "healthy"
            if rejection_rate > 0.5:  # More than 50% rejection rate
                service_status = "degraded"
            elif not all(dependencies.values()):
                service_status = "degraded" 
            
            health_status = {
                "service": "gateway_processing_service",
                "status": service_status,
                "timestamp": datetime.utcnow().isoformat(),
                "version": "1.0.0",
                "dependencies": dependencies,
                "metrics": {
                    "queries_processed": total_processed,
                    "queries_rejected": total_rejected,
                    "pii_redactions": self._metrics['pii_redactions'],
                    "rejection_rate": round(rejection_rate, 3),
                    "clarity_failure_rate": round(clarity_failure_rate, 3),
                    "reality_failure_rate": round(reality_failure_rate, 3),
                    "validation_failures": self._metrics['validation_failures'],
                    "avg_processing_time_ms": round(self._metrics['avg_processing_time_ms'], 1)
                },
                "configuration": {
                    "clarity_threshold": self._clarity_threshold,
                    "reality_threshold": self._reality_threshold,
                    "assumption_extraction_enabled": self._enable_assumption_extraction
                },
                "performance": {
                    "slo_target_p95_ms": 100,
                    "current_avg_ms": round(self._metrics['avg_processing_time_ms'], 1),
                    "slo_compliance": self._metrics['avg_processing_time_ms'] <= 100
                }
            }
            
            return health_status
            
        except Exception as e:
            self._logger.error(f"Health check failed: {e}")
            return {
                "service": "gateway_processing_service",
                "status": "unhealthy",
                "timestamp": datetime.utcnow().isoformat(),
                "error": str(e)
            }

    async def get_metrics(self) -> Dict[str, Any]:
        """
        Get current service metrics
        
        Returns:
            Current metrics snapshot
        """
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "metrics": self._metrics.copy(),
            "configuration": {
                "clarity_threshold": self._clarity_threshold,
                "reality_threshold": self._reality_threshold,
                "assumption_extraction_enabled": self._enable_assumption_extraction
            }
        }

    async def update_thresholds(self, clarity_threshold: Optional[float] = None, 
                              reality_threshold: Optional[float] = None) -> bool:
        """
        Update filtering thresholds
        
        Args:
            clarity_threshold: New clarity threshold (0.0-1.0)
            reality_threshold: New reality threshold (0.0-1.0)
            
        Returns:
            True if thresholds updated successfully
        """
        try:
            if clarity_threshold is not None:
                if 0.0 <= clarity_threshold <= 1.0:
                    self._clarity_threshold = clarity_threshold
                    self._logger.info(f"Updated clarity threshold to {clarity_threshold}")
                else:
                    raise ValueError("Clarity threshold must be between 0.0 and 1.0")
            
            if reality_threshold is not None:
                if 0.0 <= reality_threshold <= 1.0:
                    self._reality_threshold = reality_threshold
                    self._logger.info(f"Updated reality threshold to {reality_threshold}")
                else:
                    raise ValueError("Reality threshold must be between 0.0 and 1.0")
            
            return True
            
        except Exception as e:
            self._logger.error(f"Failed to update thresholds: {e}")
            return False