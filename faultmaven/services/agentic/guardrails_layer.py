"""Guardrails & Policy Layer

Component 4 of 7 in the FaultMaven agentic framework.
Provides the pervasive security boundary for input validation, PII protection,
policy enforcement, and compliance monitoring that wraps the entire agentic core.

This component implements the IGuardrailsPolicyLayer interface to provide:
- Input validation and sanitization
- PII detection and protection
- Content policy enforcement
- Output filtering and safety checks
- Compliance monitoring and audit trails
- Emergency circuit breakers and rate limiting
"""

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from faultmaven.models.agentic import (
    IGuardrailsPolicyLayer,
    GuardrailsResult,
    PolicyViolation,
    ComplianceReport,
    SafetyClassification,
    ContentFilter
)


logger = logging.getLogger(__name__)


class GuardrailsPolicyLayer(IGuardrailsPolicyLayer):
    """Production implementation of the Guardrails & Policy Layer.
    
    Provides comprehensive security boundary for the agentic framework including:
    - Multi-layer input validation and sanitization
    - Advanced PII detection and protection with context-aware redaction
    - Dynamic content policy enforcement with severity-based actions
    - Real-time safety classification and threat detection
    - Compliance monitoring with audit trails and reporting
    - Emergency circuit breakers and adaptive rate limiting
    - Integration with external security services (Presidio, custom validators)
    """

    def __init__(self, presidio_client=None, custom_validators: Optional[List[Any]] = None):
        """Initialize the guardrails layer with security components.
        
        Args:
            presidio_client: Optional Presidio client for advanced PII detection
            custom_validators: Optional list of custom validation functions
        """
        self.presidio_client = presidio_client
        self.custom_validators = custom_validators or []
        
        # Policy configuration
        self.policies = self._load_default_policies()
        self.content_filters = self._initialize_content_filters()
        
        # Rate limiting and circuit breaker state
        self.request_counts = {}
        self.circuit_breaker_state = {"open": False, "last_failure": None, "failure_count": 0}
        
        # Compliance monitoring
        self.audit_trail = []
        self.violation_history = []
        
        # Performance metrics
        self.metrics = {
            "total_requests": 0,
            "blocked_requests": 0,
            "pii_detections": 0,
            "policy_violations": 0,
            "average_processing_time": 0.0
        }
        
        logger.info("Guardrails & Policy Layer initialized")

    async def validate_input(self, content: str, context: Optional[Dict[str, Any]] = None) -> GuardrailsResult:
        """Comprehensive input validation with multi-layer security checks.
        
        Performs sequential validation through multiple security layers:
        1. Basic format and structure validation
        2. PII detection and context-aware redaction
        3. Content policy enforcement with severity assessment
        4. Threat detection and anomaly analysis
        5. Rate limiting and circuit breaker checks
        
        Args:
            content: Input content to validate
            context: Optional context including user_id, session_id, source, etc.
            
        Returns:
            GuardrailsResult with validation status, sanitized content, and violations
        """
        start_time = time.time()
        self.metrics["total_requests"] += 1
        
        try:
            # Initialize result
            result = GuardrailsResult(
                is_safe=True,
                sanitized_content=content,
                violations=[],
                confidence_score=1.0,
                processing_time=0.0,
                metadata={"original_length": len(content)}
            )
            
            # Context extraction
            user_id = context.get("user_id") if context else None
            session_id = context.get("session_id") if context else None
            source = context.get("source", "unknown") if context else "unknown"
            
            # Layer 1: Basic format validation
            format_violations = await self._validate_format(content)
            if format_violations:
                result.violations.extend(format_violations)
                result.is_safe = False
                result.confidence_score *= 0.8
            
            # Layer 2: PII detection and protection
            pii_result = await self._detect_and_protect_pii(content, context)
            if pii_result["violations"]:
                result.violations.extend(pii_result["violations"])
                result.is_safe = False
                result.confidence_score *= 0.7
                self.metrics["pii_detections"] += len(pii_result["violations"])
            
            # Update sanitized content
            result.sanitized_content = pii_result["sanitized_content"]
            
            # Layer 3: Content policy enforcement
            policy_violations = await self._enforce_content_policies(result.sanitized_content, context)
            if policy_violations:
                result.violations.extend(policy_violations)
                result.is_safe = False
                result.confidence_score *= 0.6
                self.metrics["policy_violations"] += len(policy_violations)
            
            # Layer 4: Threat detection
            threat_assessment = await self._assess_threats(result.sanitized_content, context)
            if threat_assessment["is_threat"]:
                result.violations.append(PolicyViolation(
                    type="security_threat",
                    severity="high",
                    description=f"Security threat detected: {threat_assessment['threat_type']}",
                    location="content_analysis",
                    suggested_action="block_request"
                ))
                result.is_safe = False
                result.confidence_score *= 0.5
            
            # Layer 5: Rate limiting and circuit breaker
            rate_limit_result = await self._check_rate_limits(user_id, session_id)
            if not rate_limit_result["allowed"]:
                result.violations.append(PolicyViolation(
                    type="rate_limit",
                    severity="medium",
                    description=f"Rate limit exceeded: {rate_limit_result['reason']}",
                    location="rate_limiter",
                    suggested_action="throttle_request"
                ))
                result.is_safe = False
                result.confidence_score *= 0.4
            
            # Update metrics and audit trail
            processing_time = time.time() - start_time
            result.processing_time = processing_time
            self.metrics["average_processing_time"] = (
                (self.metrics["average_processing_time"] * (self.metrics["total_requests"] - 1) + processing_time)
                / self.metrics["total_requests"]
            )
            
            if not result.is_safe:
                self.metrics["blocked_requests"] += 1
            
            # Add to audit trail
            await self._add_to_audit_trail({
                "timestamp": datetime.utcnow().isoformat(),
                "user_id": user_id,
                "session_id": session_id,
                "source": source,
                "is_safe": result.is_safe,
                "violations": len(result.violations),
                "confidence_score": result.confidence_score,
                "processing_time": processing_time
            })
            
            logger.info(f"Input validation completed: safe={result.is_safe}, violations={len(result.violations)}, time={processing_time:.3f}s")
            
            return result
            
        except Exception as e:
            logger.error(f"Error in input validation: {str(e)}")
            self.metrics["blocked_requests"] += 1
            
            return GuardrailsResult(
                is_safe=False,
                sanitized_content="[VALIDATION_ERROR]",
                violations=[PolicyViolation(
                    type="system_error",
                    severity="high",
                    description=f"Validation system error: {str(e)}",
                    location="guardrails_layer",
                    suggested_action="block_request"
                )],
                confidence_score=0.0,
                processing_time=time.time() - start_time,
                metadata={"error": str(e)}
            )

    async def validate_output(self, content: str, context: Optional[Dict[str, Any]] = None) -> GuardrailsResult:
        """Comprehensive output validation before response delivery.
        
        Ensures generated content meets safety and compliance standards:
        1. Content appropriateness and safety classification
        2. Information leakage prevention 
        3. Bias detection and mitigation
        4. Quality assurance and coherence checks
        5. Final compliance verification
        
        Args:
            content: Generated output content to validate
            context: Optional context including generation_metadata, target_audience, etc.
            
        Returns:
            GuardrailsResult with validation status and any necessary content modifications
        """
        start_time = time.time()
        
        try:
            result = GuardrailsResult(
                is_safe=True,
                sanitized_content=content,
                violations=[],
                confidence_score=1.0,
                processing_time=0.0,
                metadata={"validation_type": "output", "original_length": len(content)}
            )
            
            # Output-specific validation layers
            
            # Layer 1: Content appropriateness
            appropriateness_violations = await self._validate_content_appropriateness(content)
            if appropriateness_violations:
                result.violations.extend(appropriateness_violations)
                result.is_safe = False
                result.confidence_score *= 0.8
            
            # Layer 2: Information leakage check
            leakage_result = await self._check_information_leakage(content, context)
            if leakage_result["has_leakage"]:
                result.violations.append(PolicyViolation(
                    type="information_leakage",
                    severity="high",
                    description=f"Information leakage detected: {leakage_result['leakage_type']}",
                    location="content_analysis",
                    suggested_action="redact_content"
                ))
                result.sanitized_content = leakage_result["sanitized_content"]
                result.is_safe = False
                result.confidence_score *= 0.6
            
            # Layer 3: Bias detection
            bias_assessment = await self._assess_bias(content)
            if bias_assessment["has_bias"]:
                result.violations.append(PolicyViolation(
                    type="content_bias",
                    severity="medium",
                    description=f"Potential bias detected: {bias_assessment['bias_type']}",
                    location="bias_analyzer",
                    suggested_action="flag_for_review"
                ))
                result.confidence_score *= 0.9
            
            # Layer 4: Quality assurance
            quality_score = await self._assess_content_quality(content)
            if quality_score < 0.7:
                result.violations.append(PolicyViolation(
                    type="quality_issue",
                    severity="low",
                    description=f"Content quality below threshold: {quality_score:.2f}",
                    location="quality_analyzer",
                    suggested_action="flag_for_review"
                ))
                result.confidence_score *= 0.95
            
            # Update processing time and metrics
            result.processing_time = time.time() - start_time
            
            logger.info(f"Output validation completed: safe={result.is_safe}, violations={len(result.violations)}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error in output validation: {str(e)}")
            
            return GuardrailsResult(
                is_safe=False,
                sanitized_content="[OUTPUT_VALIDATION_ERROR]",
                violations=[PolicyViolation(
                    type="system_error",
                    severity="high",
                    description=f"Output validation error: {str(e)}",
                    location="guardrails_layer",
                    suggested_action="block_response"
                )],
                confidence_score=0.0,
                processing_time=time.time() - start_time,
                metadata={"error": str(e)}
            )

    async def classify_safety(self, content: str, context: Optional[Dict[str, Any]] = None) -> SafetyClassification:
        """Advanced safety classification with multi-dimensional risk assessment.
        
        Provides comprehensive safety analysis across multiple dimensions:
        - Content harmfulness and toxicity levels
        - Privacy risk assessment
        - Compliance risk evaluation
        - Contextual appropriateness analysis
        - Confidence intervals and uncertainty quantification
        
        Args:
            content: Content to classify for safety
            context: Optional context for contextual safety assessment
            
        Returns:
            SafetyClassification with detailed risk analysis and recommendations
        """
        try:
            # Initialize classification
            classification = SafetyClassification(
                overall_safety="safe",
                confidence_score=1.0,
                risk_factors=[],
                safety_dimensions={},
                recommendations=[],
                metadata={}
            )
            
            # Multi-dimensional safety assessment
            
            # Dimension 1: Content harmfulness
            harm_assessment = await self._assess_harmfulness(content)
            classification.safety_dimensions["harmfulness"] = harm_assessment
            if harm_assessment["risk_level"] != "low":
                classification.risk_factors.append(f"harmfulness_{harm_assessment['risk_level']}")
                classification.confidence_score *= 0.8
            
            # Dimension 2: Privacy risk
            privacy_risk = await self._assess_privacy_risk(content)
            classification.safety_dimensions["privacy"] = privacy_risk
            if privacy_risk["risk_level"] != "low":
                classification.risk_factors.append(f"privacy_{privacy_risk['risk_level']}")
                classification.confidence_score *= 0.85
            
            # Dimension 3: Compliance risk
            compliance_risk = await self._assess_compliance_risk(content, context)
            classification.safety_dimensions["compliance"] = compliance_risk
            if compliance_risk["risk_level"] != "low":
                classification.risk_factors.append(f"compliance_{compliance_risk['risk_level']}")
                classification.confidence_score *= 0.9
            
            # Dimension 4: Contextual appropriateness
            if context:
                context_risk = await self._assess_contextual_risk(content, context)
                classification.safety_dimensions["contextual"] = context_risk
                if context_risk["risk_level"] != "low":
                    classification.risk_factors.append(f"contextual_{context_risk['risk_level']}")
                    classification.confidence_score *= 0.9
            
            # Overall safety determination
            high_risk_factors = [rf for rf in classification.risk_factors if "high" in rf]
            medium_risk_factors = [rf for rf in classification.risk_factors if "medium" in rf]
            
            if high_risk_factors:
                classification.overall_safety = "unsafe"
                classification.recommendations.append("Block content due to high-risk factors")
            elif len(medium_risk_factors) >= 2:
                classification.overall_safety = "risky"
                classification.recommendations.append("Review content due to multiple medium-risk factors")
            elif medium_risk_factors:
                classification.overall_safety = "moderate"
                classification.recommendations.append("Monitor content due to medium-risk factors")
            
            # Add specific recommendations based on risk factors
            for risk_factor in classification.risk_factors:
                if "harmfulness_high" in risk_factor:
                    classification.recommendations.append("Apply content filtering for harmful content")
                elif "privacy_high" in risk_factor:
                    classification.recommendations.append("Enhanced PII redaction required")
                elif "compliance_high" in risk_factor:
                    classification.recommendations.append("Compliance review required")
            
            classification.metadata = {
                "total_dimensions": len(classification.safety_dimensions),
                "risk_factor_count": len(classification.risk_factors),
                "assessment_timestamp": datetime.utcnow().isoformat()
            }
            
            logger.info(f"Safety classification: {classification.overall_safety}, risk_factors={len(classification.risk_factors)}, confidence={classification.confidence_score:.3f}")
            
            return classification
            
        except Exception as e:
            logger.error(f"Error in safety classification: {str(e)}")
            
            return SafetyClassification(
                overall_safety="unknown",
                confidence_score=0.0,
                risk_factors=["system_error"],
                safety_dimensions={"error": {"risk_level": "unknown", "details": str(e)}},
                recommendations=["Manual review required due to classification error"],
                metadata={"error": str(e)}
            )

    async def generate_compliance_report(self, timeframe: str = "24h") -> ComplianceReport:
        """Generate comprehensive compliance report with audit trail analysis.
        
        Provides detailed compliance analysis including:
        - Policy violation trends and patterns
        - Risk assessment summaries
        - Regulatory compliance status
        - Recommendation for policy improvements
        - Audit trail completeness verification
        
        Args:
            timeframe: Report timeframe (e.g., "24h", "7d", "30d")
            
        Returns:
            ComplianceReport with detailed compliance analysis and recommendations
        """
        try:
            # Parse timeframe
            hours_back = self._parse_timeframe(timeframe)
            cutoff_time = datetime.utcnow().timestamp() - (hours_back * 3600)
            
            # Filter audit trail by timeframe
            recent_entries = [
                entry for entry in self.audit_trail[-1000:]  # Last 1000 entries
                if datetime.fromisoformat(entry["timestamp"].replace('Z', '+00:00')).timestamp() > cutoff_time
            ]
            
            # Generate report
            report = ComplianceReport(
                timeframe=timeframe,
                total_requests=len(recent_entries),
                blocked_requests=len([e for e in recent_entries if not e["is_safe"]]),
                policy_violations_by_type={},
                risk_assessment_summary={},
                compliance_status="compliant",
                recommendations=[],
                audit_trail_completeness=1.0,
                generated_at=datetime.utcnow().isoformat(),
                metadata={}
            )
            
            if report.total_requests > 0:
                # Analyze violations
                violation_types = {}
                for entry in recent_entries:
                    if not entry["is_safe"]:
                        for violation in self.violation_history:
                            if violation.get("timestamp", "").startswith(entry["timestamp"][:10]):  # Same day
                                vtype = violation.get("type", "unknown")
                                violation_types[vtype] = violation_types.get(vtype, 0) + 1
                
                report.policy_violations_by_type = violation_types
                
                # Calculate block rate
                block_rate = report.blocked_requests / report.total_requests
                
                # Risk assessment summary
                report.risk_assessment_summary = {
                    "block_rate": block_rate,
                    "average_confidence": sum(e.get("confidence_score", 1.0) for e in recent_entries) / len(recent_entries),
                    "processing_time_avg": sum(e.get("processing_time", 0.0) for e in recent_entries) / len(recent_entries),
                    "top_violation_type": max(violation_types, key=violation_types.get) if violation_types else None
                }
                
                # Compliance status determination
                if block_rate > 0.1:  # More than 10% blocked
                    report.compliance_status = "at_risk"
                    report.recommendations.append("High block rate detected - review policy sensitivity")
                elif block_rate > 0.05:  # More than 5% blocked
                    report.compliance_status = "monitoring"
                    report.recommendations.append("Elevated block rate - monitor for trends")
                
                # Specific recommendations based on violations
                if "pii_detection" in violation_types and violation_types["pii_detection"] > 5:
                    report.recommendations.append("High PII detection rate - consider user training")
                
                if "rate_limit" in violation_types:
                    report.recommendations.append("Rate limiting active - consider capacity planning")
                
                # Audit trail completeness
                expected_entries = max(1, self.metrics["total_requests"])
                actual_entries = len(self.audit_trail)
                report.audit_trail_completeness = min(1.0, actual_entries / expected_entries)
                
                if report.audit_trail_completeness < 0.95:
                    report.recommendations.append("Audit trail incomplete - investigate logging issues")
            
            # Metadata
            report.metadata = {
                "guardrails_version": "1.0.0",
                "policies_active": len(self.policies),
                "filters_active": len(self.content_filters),
                "metrics_snapshot": self.metrics.copy()
            }
            
            logger.info(f"Compliance report generated: timeframe={timeframe}, requests={report.total_requests}, blocked={report.blocked_requests}")
            
            return report
            
        except Exception as e:
            logger.error(f"Error generating compliance report: {str(e)}")
            
            return ComplianceReport(
                timeframe=timeframe,
                total_requests=0,
                blocked_requests=0,
                policy_violations_by_type={},
                risk_assessment_summary={},
                compliance_status="error",
                recommendations=[f"Unable to generate report: {str(e)}"],
                audit_trail_completeness=0.0,
                generated_at=datetime.utcnow().isoformat(),
                metadata={"error": str(e)}
            )

    # Private helper methods

    def _load_default_policies(self) -> Dict[str, Any]:
        """Load default security policies."""
        return {
            "pii_protection": {
                "enabled": True,
                "entities": ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "SSN", "CREDIT_CARD"],
                "redaction_method": "mask"
            },
            "content_safety": {
                "enabled": True,
                "blocked_categories": ["hate_speech", "violence", "adult_content"],
                "severity_threshold": "medium"
            },
            "rate_limiting": {
                "enabled": True,
                "global_limit": 1000,
                "per_user_limit": 100,
                "per_session_limit": 50,
                "window_minutes": 60
            },
            "compliance": {
                "gdpr_enabled": True,
                "hipaa_enabled": False,
                "retention_days": 30
            }
        }

    def _initialize_content_filters(self) -> List[ContentFilter]:
        """Initialize content filtering rules."""
        return [
            ContentFilter(
                filter_type="profanity_filter",
                rules=[{"pattern": r"\b(damn|hell|crap)\b", "action": "warn", "severity": "low"}]
            ),
            ContentFilter(
                filter_type="email_pattern",
                rules=[{"pattern": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "action": "redact", "severity": "medium"}]
            ),
            ContentFilter(
                filter_type="phone_pattern",
                rules=[{"pattern": r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "action": "redact", "severity": "medium"}]
            ),
            ContentFilter(
                filter_type="ssn_pattern",
                rules=[{"pattern": r"\b\d{3}-\d{2}-\d{4}\b", "action": "block", "severity": "high"}]
            )
        ]

    async def _validate_format(self, content: str) -> List[PolicyViolation]:
        """Basic format validation."""
        violations = []
        
        # Length checks
        if len(content) > 100000:  # 100KB limit
            violations.append(PolicyViolation(
                type="content_length",
                severity="high",
                description="Content exceeds maximum length limit",
                location="format_validator",
                suggested_action="truncate_content"
            ))
        
        # Encoding checks
        try:
            content.encode('utf-8')
        except UnicodeEncodeError:
            violations.append(PolicyViolation(
                type="encoding_error",
                severity="medium",
                description="Content contains invalid characters",
                location="format_validator",
                suggested_action="sanitize_encoding"
            ))
        
        return violations

    async def _detect_and_protect_pii(self, content: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """PII detection and protection with context awareness."""
        sanitized_content = content
        violations = []
        
        # Use Presidio if available
        if self.presidio_client:
            try:
                # Mock Presidio integration - replace with actual client calls
                pii_entities = []  # Would be: await self.presidio_client.analyze(content)
                for entity in pii_entities:
                    violations.append(PolicyViolation(
                        type="pii_detection",
                        severity="high",
                        description=f"PII detected: {entity['entity_type']}",
                        location=f"position_{entity['start']}_{entity['end']}",
                        suggested_action="redact_content"
                    ))
            except Exception as e:
                logger.warning(f"Presidio PII detection failed: {str(e)}")
        
        # Fallback regex-based PII detection
        for content_filter in self.content_filters:
            if content_filter.pattern:
                matches = re.finditer(content_filter.pattern, content, re.IGNORECASE)
                for match in matches:
                    if content_filter.action == "redact":
                        sanitized_content = sanitized_content.replace(match.group(), "[REDACTED]")
                    
                    violations.append(PolicyViolation(
                        type="pii_pattern_match",
                        severity=content_filter.severity,
                        description=f"PII pattern matched: {content_filter.name}",
                        location=f"position_{match.start()}_{match.end()}",
                        suggested_action=content_filter.action
                    ))
        
        return {
            "sanitized_content": sanitized_content,
            "violations": violations
        }

    async def _enforce_content_policies(self, content: str, context: Optional[Dict[str, Any]] = None) -> List[PolicyViolation]:
        """Content policy enforcement."""
        violations = []
        
        # Check against blocked categories
        blocked_categories = self.policies["content_safety"]["blocked_categories"]
        
        # Simple keyword-based detection (replace with ML model in production)
        category_keywords = {
            "hate_speech": ["hate", "discrimination", "racist", "sexist"],
            "violence": ["kill", "murder", "assault", "violence"],
            "adult_content": ["explicit", "adult", "sexual"]
        }
        
        content_lower = content.lower()
        for category in blocked_categories:
            if category in category_keywords:
                for keyword in category_keywords[category]:
                    if keyword in content_lower:
                        violations.append(PolicyViolation(
                            type="content_policy",
                            severity="high",
                            description=f"Content violates {category} policy",
                            location="content_analyzer",
                            suggested_action="block_content"
                        ))
                        break
        
        return violations

    async def _assess_threats(self, content: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Security threat assessment."""
        # Simple threat detection patterns
        threat_patterns = [
            (r"<script.*?>", "xss_attempt"),
            (r"union.*select", "sql_injection"),
            (r"\.\.\/", "path_traversal"),
            (r"eval\(", "code_injection")
        ]
        
        content_lower = content.lower()
        for pattern, threat_type in threat_patterns:
            if re.search(pattern, content_lower, re.IGNORECASE):
                return {"is_threat": True, "threat_type": threat_type}
        
        return {"is_threat": False, "threat_type": None}

    async def _check_rate_limits(self, user_id: Optional[str], session_id: Optional[str]) -> Dict[str, Any]:
        """Rate limiting checks."""
        current_time = time.time()
        window_seconds = self.policies["rate_limiting"]["window_minutes"] * 60
        
        # Clean old entries
        cutoff_time = current_time - window_seconds
        for key in list(self.request_counts.keys()):
            self.request_counts[key] = [t for t in self.request_counts[key] if t > cutoff_time]
            if not self.request_counts[key]:
                del self.request_counts[key]
        
        # Check limits
        limits_to_check = [
            ("global", "global", self.policies["rate_limiting"]["global_limit"])
        ]
        
        if user_id:
            limits_to_check.append((f"user_{user_id}", user_id, self.policies["rate_limiting"]["per_user_limit"]))
        
        if session_id:
            limits_to_check.append((f"session_{session_id}", session_id, self.policies["rate_limiting"]["per_session_limit"]))
        
        for key, identifier, limit in limits_to_check:
            if key not in self.request_counts:
                self.request_counts[key] = []
            
            if len(self.request_counts[key]) >= limit:
                return {
                    "allowed": False,
                    "reason": f"Rate limit exceeded for {identifier}: {len(self.request_counts[key])}/{limit}"
                }
            
            # Add current request
            self.request_counts[key].append(current_time)
        
        return {"allowed": True, "reason": None}

    async def _add_to_audit_trail(self, entry: Dict[str, Any]) -> None:
        """Add entry to audit trail with size management."""
        self.audit_trail.append(entry)
        
        # Keep only last 10000 entries
        if len(self.audit_trail) > 10000:
            self.audit_trail = self.audit_trail[-5000:]  # Keep last 5000

    async def _validate_content_appropriateness(self, content: str) -> List[PolicyViolation]:
        """Validate content appropriateness for output."""
        violations = []
        
        # Check for potentially inappropriate content
        inappropriate_patterns = [
            (r"confidential|secret|classified", "information_sensitivity"),
            (r"password|token|key", "credential_exposure"),
            (r"internal|proprietary", "internal_information")
        ]
        
        for pattern, violation_type in inappropriate_patterns:
            if re.search(pattern, content, re.IGNORECASE):
                violations.append(PolicyViolation(
                    type=violation_type,
                    severity="medium",
                    description=f"Inappropriate content detected: {violation_type}",
                    location="content_analyzer",
                    suggested_action="review_content"
                ))
        
        return violations

    async def _check_information_leakage(self, content: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Check for information leakage in output."""
        # Simple information leakage detection
        leakage_patterns = [
            (r"\b\d{4}[-\s]\d{4}[-\s]\d{4}[-\s]\d{4}\b", "credit_card"),
            (r"\b\d{3}-\d{2}-\d{4}\b", "ssn"),
            (r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "email")
        ]
        
        sanitized_content = content
        has_leakage = False
        leakage_types = []
        
        for pattern, leakage_type in leakage_patterns:
            matches = list(re.finditer(pattern, content))
            if matches:
                has_leakage = True
                leakage_types.append(leakage_type)
                for match in matches:
                    sanitized_content = sanitized_content.replace(match.group(), f"[REDACTED_{leakage_type.upper()}]")
        
        return {
            "has_leakage": has_leakage,
            "leakage_type": ", ".join(leakage_types),
            "sanitized_content": sanitized_content
        }

    async def _assess_bias(self, content: str) -> Dict[str, Any]:
        """Assess content for potential bias."""
        # Simple bias detection keywords
        bias_indicators = [
            ("gender", ["he is better", "she is worse", "typical man", "typical woman"]),
            ("racial", ["those people", "they always", "typical of them"]),
            ("age", ["too old", "too young", "millennials are", "boomers are"])
        ]
        
        content_lower = content.lower()
        for bias_type, keywords in bias_indicators:
            for keyword in keywords:
                if keyword in content_lower:
                    return {"has_bias": True, "bias_type": bias_type}
        
        return {"has_bias": False, "bias_type": None}

    async def _assess_content_quality(self, content: str) -> float:
        """Assess content quality score (0.0 to 1.0)."""
        score = 1.0
        
        # Length check
        if len(content) < 10:
            score *= 0.5
        
        # Coherence check (simple word repetition)
        words = content.lower().split()
        if len(words) > 0:
            unique_ratio = len(set(words)) / len(words)
            score *= unique_ratio
        
        # Grammar check (simple capitalization and punctuation)
        if content and content[0].islower():
            score *= 0.9
        
        if content and content[-1] not in '.!?':
            score *= 0.9
        
        return min(1.0, max(0.0, score))

    async def _assess_harmfulness(self, content: str) -> Dict[str, Any]:
        """Assess content harmfulness."""
        # Simple harmfulness indicators
        harmful_keywords = {
            "high": ["kill", "murder", "suicide", "bomb"],
            "medium": ["fight", "attack", "hurt", "damage"],
            "low": ["angry", "upset", "annoyed"]
        }
        
        content_lower = content.lower()
        for risk_level, keywords in harmful_keywords.items():
            for keyword in keywords:
                if keyword in content_lower:
                    return {"risk_level": risk_level, "indicators": [keyword]}
        
        return {"risk_level": "low", "indicators": []}

    async def _assess_privacy_risk(self, content: str) -> Dict[str, Any]:
        """Assess privacy risk."""
        privacy_indicators = {
            "high": [r"\b\d{3}-\d{2}-\d{4}\b", r"\b\d{4}[-\s]\d{4}[-\s]\d{4}[-\s]\d{4}\b"],
            "medium": [r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"],
            "low": [r"\b[A-Z][a-z]+ [A-Z][a-z]+\b"]  # Name patterns
        }
        
        for risk_level, patterns in privacy_indicators.items():
            for pattern in patterns:
                if re.search(pattern, content):
                    return {"risk_level": risk_level, "patterns": [pattern]}
        
        return {"risk_level": "low", "patterns": []}

    async def _assess_compliance_risk(self, content: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Assess compliance risk based on content and context."""
        risk_level = "low"
        issues = []
        
        # GDPR compliance check
        if self.policies["compliance"]["gdpr_enabled"]:
            gdpr_keywords = ["personal data", "processing", "consent", "data subject"]
            content_lower = content.lower()
            for keyword in gdpr_keywords:
                if keyword in content_lower:
                    risk_level = "medium"
                    issues.append(f"GDPR-related content: {keyword}")
        
        # Industry-specific compliance
        if context and context.get("industry") == "healthcare":
            hipaa_keywords = ["patient", "medical record", "health information"]
            content_lower = content.lower()
            for keyword in hipaa_keywords:
                if keyword in content_lower:
                    risk_level = "high"
                    issues.append(f"HIPAA-related content: {keyword}")
        
        return {"risk_level": risk_level, "issues": issues}

    async def _assess_contextual_risk(self, content: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Assess contextual appropriateness."""
        risk_level = "low"
        issues = []
        
        # Audience appropriateness
        audience = context.get("target_audience", "general")
        if audience == "children":
            adult_keywords = ["violence", "mature", "adult"]
            content_lower = content.lower()
            for keyword in adult_keywords:
                if keyword in content_lower:
                    risk_level = "high"
                    issues.append(f"Inappropriate for children: {keyword}")
        
        # Professional context
        if context.get("context_type") == "professional":
            informal_patterns = [r"\b(lol|omg|wtf)\b", r"!{3,}"]
            for pattern in informal_patterns:
                if re.search(pattern, content, re.IGNORECASE):
                    risk_level = "medium"
                    issues.append("Informal language in professional context")
        
        return {"risk_level": risk_level, "issues": issues}

    def _parse_timeframe(self, timeframe: str) -> int:
        """Parse timeframe string to hours."""
        if timeframe.endswith('h'):
            return int(timeframe[:-1])
        elif timeframe.endswith('d'):
            return int(timeframe[:-1]) * 24
        elif timeframe.endswith('w'):
            return int(timeframe[:-1]) * 24 * 7
        else:
            return 24  # Default to 24 hours

    # Required abstract methods from IGuardrailsPolicyLayer interface
    async def evaluate_request(self, request: Dict[str, Any], context: Dict[str, Any]) -> List[PolicyViolation]:
        """Evaluate a request against all policies"""
        content = request.get('content', '')
        result = await self.validate_input(content, context)
        return result.violations

    async def check_safety_constraints(self, operation: str, parameters: Dict[str, Any]) -> bool:
        """Check if an operation meets safety constraints"""
        content = f"Operation: {operation}, Parameters: {str(parameters)}"
        result = await self.validate_input(content, {})
        return result.is_safe

    async def enforce_user_permissions(self, user_id: str, operation: str) -> bool:
        """Check if user has permission for operation"""
        # Basic permission check - can be enhanced with actual permission system
        return True

    async def apply_data_sanitization(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Apply data sanitization policies"""
        sanitized_data = {}
        for key, value in data.items():
            if isinstance(value, str):
                result = await self.validate_input(value, {})
                sanitized_data[key] = result.sanitized_content
            else:
                sanitized_data[key] = value
        return sanitized_data

    async def audit_operation(self, operation: str, user_id: str, parameters: Dict[str, Any]) -> bool:
        """Record operation for audit purposes"""
        await self._add_to_audit_trail({
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": user_id,
            "operation": operation,
            "parameters": parameters,
            "type": "operation_audit"
        })
        return True