"""Enhanced Security Assessment - Phase 3

Purpose: Pattern-based security assessment with memory integration and learning

This enhanced security module builds upon the existing DataSanitizer to provide:
- Pattern-based PII detection with learned patterns
- Memory-aware security assessment using conversation context
- Adaptive security rules based on user feedback
- Cross-session security pattern learning
- Enhanced threat detection with context awareness

Core Responsibilities:
- Memory-enhanced PII detection and classification
- Pattern learning from security feedback
- Context-aware security rule application
- Adaptive threat assessment
- Security insight extraction with historical context
- Privacy risk assessment with user expertise consideration

Key Enhancements:
- Memory service integration for security context
- Pattern learner for continuous security improvement
- Context-aware PII detection rules
- Adaptive security assessment based on user domain
- Security pattern sharing across sessions
"""

import logging
import re
import time
import hashlib
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from collections import defaultdict, deque
from enum import Enum

from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.models.interfaces import IMemoryService, ISanitizer, ConversationContext
from faultmaven.core.processing.pattern_learner import PatternLearner, PatternType, Pattern
from faultmaven.infrastructure.observability.tracing import trace


class SecurityRiskLevel(Enum):
    """Security risk levels for assessment"""
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PIICategory(Enum):
    """Categories of PII for fine-grained detection"""
    PERSONAL_IDENTIFIER = "personal_identifier"
    FINANCIAL = "financial"
    HEALTH = "health"
    TECHNICAL = "technical"
    CREDENTIAL = "credential"
    BIOMETRIC = "biometric"
    LOCATION = "location"


@dataclass
class SecurityFinding:
    """Individual security finding with detailed information"""
    finding_id: str
    category: PIICategory
    risk_level: SecurityRiskLevel
    pattern_matched: str
    context_snippet: str
    confidence: float
    position: Tuple[int, int]  # start, end
    remediation: str
    learned_pattern: bool


@dataclass
class SecurityAssessmentResult:
    """Comprehensive security assessment result"""
    assessment_id: str
    session_id: str
    content_hash: str
    overall_risk_level: SecurityRiskLevel
    total_findings: int
    findings_by_category: Dict[PIICategory, int]
    findings_by_risk: Dict[SecurityRiskLevel, int]
    detailed_findings: List[SecurityFinding]
    sanitized_content: str
    memory_enhanced: bool
    patterns_applied: List[str]
    recommendations: List[str]
    processing_time_ms: float


class EnhancedSecurityAssessment:
    """
    Enhanced security assessment system with memory integration and pattern learning
    
    This system provides comprehensive security analysis capabilities including:
    - Memory-aware PII detection using conversation context
    - Pattern learning from user feedback and corrections
    - Context-driven security rule application
    - Adaptive risk assessment based on user expertise
    - Cross-session security pattern sharing
    """
    
    def __init__(
        self,
        memory_service: Optional[IMemoryService] = None,
        data_sanitizer: Optional[ISanitizer] = None,
        pattern_learner: Optional[PatternLearner] = None
    ):
        """
        Initialize Enhanced Security Assessment with integrated services
        
        Args:
            memory_service: Memory service for context retrieval
            data_sanitizer: Base data sanitizer for PII detection
            pattern_learner: Pattern learning service for security patterns
        """
        self.logger = logging.getLogger(__name__)
        
        # Core services
        self._memory_service = memory_service
        self._data_sanitizer = data_sanitizer or DataSanitizer()
        self._pattern_learner = pattern_learner or PatternLearner(memory_service)
        
        # Enhanced security patterns organized by category
        self._enhanced_security_patterns = {
            PIICategory.PERSONAL_IDENTIFIER: [
                # Enhanced email patterns
                (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", 0.9, "Email address"),
                # Phone numbers (multiple formats)
                (r"\b(?:\+?1[-.\s]?)?\(?([0-9]{3})\)?[-.\s]?([0-9]{3})[-.\s]?([0-9]{4})\b", 0.85, "Phone number"),
                (r"\b\d{3}-\d{3}-\d{4}\b", 0.9, "US phone number"),
                # SSN patterns
                (r"\b\d{3}-\d{2}-\d{4}\b", 0.95, "Social Security Number"),
                (r"\b\d{3}\s\d{2}\s\d{4}\b", 0.9, "Social Security Number (spaced)"),
                # Driver's license
                (r"\b[A-Z]{1,2}\d{6,8}\b", 0.6, "Driver's license number"),
                # Passport numbers
                (r"\b[A-Z0-9]{6,9}\b", 0.4, "Potential passport number"),
            ],
            PIICategory.FINANCIAL: [
                # Credit card numbers
                (r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|3[0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12})\b", 0.95, "Credit card number"),
                # Banking account numbers
                (r"\b\d{10,17}\b", 0.3, "Potential account number"),
                # Routing numbers
                (r"\b\d{9}\b", 0.4, "Potential routing number"),
                # IBAN
                (r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}([A-Z0-9]?){0,16}\b", 0.8, "IBAN"),
            ],
            PIICategory.HEALTH: [
                # Medical record numbers
                (r"\bMRN[-#]?\s*\d{6,10}\b", 0.85, "Medical record number"),
                # Health insurance numbers
                (r"\b[A-Z]{3}\d{6,9}\b", 0.5, "Potential health insurance number"),
                # Prescription numbers
                (r"\bRx[-#]?\s*\d{6,12}\b", 0.7, "Prescription number"),
            ],
            PIICategory.TECHNICAL: [
                # IP addresses (enhanced)
                (r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b", 0.8, "IP address"),
                # MAC addresses
                (r"\b([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})\b", 0.9, "MAC address"),
                # UUIDs
                (r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", 0.7, "UUID"),
                # Container IDs
                (r"\b[0-9a-fA-F]{64}\b", 0.6, "Container/Docker ID"),
                (r"\b[0-9a-fA-F]{12}\b", 0.4, "Short container ID"),
            ],
            PIICategory.CREDENTIAL: [
                # API keys (enhanced patterns)
                (r"sk-[0-9a-zA-Z]{48}", 0.95, "OpenAI API key"),
                (r"pk-[0-9a-zA-Z]{48}", 0.95, "OpenAI public key"),
                (r"AKIA[0-9A-Z]{16}", 0.95, "AWS access key"),
                (r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}", 0.9, "GitHub token"),
                # Generic API keys
                (r"(?:api[_-]?key|apikey)[_-]?[=:]\s*['\"]?[A-Za-z0-9]{16,}['\"]?", 0.8, "API key"),
                # JWT tokens
                (r"eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*", 0.85, "JWT token"),
                # Bearer tokens
                (r"Bearer\s+[A-Za-z0-9._-]+", 0.8, "Bearer token"),
                # Passwords in URLs
                (r"://[^:/@]+:[^:/@]+@", 0.9, "Credentials in URL"),
                # Database connection strings
                (r"(?:mongodb|postgresql|mysql)://[^@/\s]+:[^@/\s]+@[^/\s]+", 0.9, "Database connection string"),
            ],
            PIICategory.LOCATION: [
                # Coordinates
                (r"\b-?\d{1,3}\.\d{1,7},\s*-?\d{1,3}\.\d{1,7}\b", 0.7, "GPS coordinates"),
                # Zip codes
                (r"\b\d{5}(?:-\d{4})?\b", 0.4, "ZIP code"),
                # Address patterns (basic)
                (r"\b\d{1,5}\s+[\w\s]+(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Boulevard|Blvd)\b", 0.6, "Street address"),
            ],
            PIICategory.BIOMETRIC: [
                # Placeholder for biometric data patterns
                # These would be highly specialized and context-dependent
            ]
        }
        
        # Compile patterns for efficiency
        self._compiled_patterns = {}
        for category, patterns in self._enhanced_security_patterns.items():
            self._compiled_patterns[category] = [
                (re.compile(pattern, re.IGNORECASE), confidence, description)
                for pattern, confidence, description in patterns
            ]
        
        # Assessment history and metrics
        self._assessment_history = deque(maxlen=1000)
        self._security_metrics = {
            "assessments_performed": 0,
            "memory_enhanced_assessments": 0,
            "total_findings": 0,
            "high_risk_findings": 0,
            "patterns_learned": 0,
            "avg_assessment_time": 0.0,
            "avg_risk_level": 0.0
        }
        
        # Risk level scoring
        self._risk_scores = {
            SecurityRiskLevel.NONE: 0,
            SecurityRiskLevel.LOW: 1,
            SecurityRiskLevel.MEDIUM: 2,
            SecurityRiskLevel.HIGH: 3,
            SecurityRiskLevel.CRITICAL: 4
        }
    
    @trace("enhanced_security_assessment_assess")
    async def assess_security(
        self,
        content: str,
        session_id: str,
        context: Optional[Dict[str, Any]] = None
    ) -> SecurityAssessmentResult:
        """
        Perform comprehensive security assessment with memory integration
        
        Args:
            content: Content to assess for security issues
            session_id: Session identifier for memory context
            context: Additional context for assessment
            
        Returns:
            SecurityAssessmentResult with detailed security analysis
        """
        start_time = time.time()
        assessment_id = self._generate_assessment_id(content)
        
        try:
            # Retrieve memory context for enhanced assessment
            memory_context = None
            memory_enhanced = False
            
            if self._memory_service and session_id:
                try:
                    # Get conversation context for security-aware assessment
                    content_preview = content[:200] + "..." if len(content) > 200 else content
                    memory_context = await self._memory_service.retrieve_context(
                        session_id, f"security assessment: {content_preview}"
                    )
                    memory_enhanced = True
                except Exception as e:
                    self.logger.warning(f"Failed to retrieve memory context for security assessment: {e}")
            
            # Extract security context insights
            security_context = self._extract_security_context(memory_context, context)
            
            # Apply base sanitizer for known patterns
            base_sanitized = self._data_sanitizer.sanitize(content)
            
            # Perform enhanced pattern-based detection
            detailed_findings = await self._perform_enhanced_detection(
                content, security_context
            )
            
            # Apply learned security patterns
            learned_findings = await self._apply_learned_security_patterns(
                content, security_context
            )
            detailed_findings.extend(learned_findings)
            
            # Assess risk levels with context awareness
            risk_assessed_findings = self._assess_risk_levels(
                detailed_findings, security_context
            )
            
            # Calculate overall risk level
            overall_risk = self._calculate_overall_risk(risk_assessed_findings)
            
            # Generate sanitized content
            sanitized_content = self._generate_enhanced_sanitized_content(
                content, risk_assessed_findings
            )
            
            # Generate recommendations
            recommendations = self._generate_security_recommendations(
                risk_assessed_findings, security_context, memory_context
            )
            
            # Compile assessment result
            result = SecurityAssessmentResult(
                assessment_id=assessment_id,
                session_id=session_id,
                content_hash=self._hash_content(content),
                overall_risk_level=overall_risk,
                total_findings=len(risk_assessed_findings),
                findings_by_category=self._group_findings_by_category(risk_assessed_findings),
                findings_by_risk=self._group_findings_by_risk(risk_assessed_findings),
                detailed_findings=risk_assessed_findings,
                sanitized_content=sanitized_content,
                memory_enhanced=memory_enhanced,
                patterns_applied=self._get_applied_patterns(risk_assessed_findings),
                recommendations=recommendations,
                processing_time_ms=(time.time() - start_time) * 1000
            )
            
            # Update metrics and history
            self._update_security_metrics(result)
            self._assessment_history.append({
                "assessment_id": assessment_id,
                "session_id": session_id,
                "result": result,
                "timestamp": time.time()
            })
            
            # Log assessment completion
            self.logger.info(
                f"Security assessment completed: {len(risk_assessed_findings)} findings, "
                f"risk level: {overall_risk.value}, time: {result.processing_time_ms:.2f}ms"
            )
            
            return result
            
        except Exception as e:
            self.logger.error(f"Security assessment failed: {e}")
            # Return minimal result with error information
            return SecurityAssessmentResult(
                assessment_id=assessment_id,
                session_id=session_id,
                content_hash=self._hash_content(content),
                overall_risk_level=SecurityRiskLevel.MEDIUM,  # Conservative default
                total_findings=0,
                findings_by_category={},
                findings_by_risk={},
                detailed_findings=[],
                sanitized_content=content,  # Return original if processing fails
                memory_enhanced=False,
                patterns_applied=[],
                recommendations=["Security assessment failed - manual review recommended"],
                processing_time_ms=(time.time() - start_time) * 1000
            )
    
    async def learn_security_patterns(
        self,
        content: str,
        assessment_result: SecurityAssessmentResult,
        user_feedback: Dict[str, Any],
        session_id: str
    ) -> Dict[str, Any]:
        """
        Learn security patterns from user feedback
        
        Args:
            content: Original content that was assessed
            assessment_result: Security assessment result
            user_feedback: User corrections and feedback
            session_id: Session identifier
            
        Returns:
            Dictionary with learning results
        """
        try:
            # Extract security-specific feedback
            security_feedback = self._extract_security_feedback(
                user_feedback, assessment_result
            )
            
            # Learn patterns using the pattern learner
            learning_result = await self._pattern_learner.learn_from_feedback(
                content=content,
                predicted_result={
                    "security_assessment": assessment_result,
                    "findings": assessment_result.detailed_findings
                },
                actual_result=security_feedback,
                user_feedback=user_feedback,
                session_id=session_id,
                context={"domain": "security", "assessment_id": assessment_result.assessment_id}
            )
            
            # Update security metrics
            self._security_metrics["patterns_learned"] += learning_result.patterns_learned
            
            return {
                "patterns_learned": learning_result.patterns_learned,
                "patterns_updated": learning_result.patterns_updated,
                "learning_confidence": learning_result.learning_confidence,
                "processing_time_ms": learning_result.processing_time_ms
            }
            
        except Exception as e:
            self.logger.error(f"Security pattern learning failed: {e}")
            return {"error": str(e)}
    
    def _extract_security_context(
        self,
        memory_context: Optional[ConversationContext],
        additional_context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Extract security-relevant context from memory and additional context"""
        security_context = {
            "user_expertise": "unknown",
            "domain_focus": [],
            "data_sensitivity": "medium",
            "compliance_requirements": [],
            "previous_security_issues": [],
            "risk_tolerance": "medium"
        }
        
        # Extract from memory context
        if memory_context:
            # User expertise affects risk assessment
            if memory_context.user_profile:
                security_context["user_expertise"] = memory_context.user_profile.get(
                    "skill_level", "unknown"
                )
            
            # Extract domain focus for context-aware assessment
            if memory_context.domain_context:
                security_context["domain_focus"] = list(memory_context.domain_context.keys())
            
            # Analyze conversation history for security indicators
            if memory_context.conversation_history:
                for item in memory_context.conversation_history[-5:]:
                    content_lower = item.get("content", "").lower()
                    
                    # Identify compliance requirements
                    if any(term in content_lower for term in ["gdpr", "hipaa", "pci", "sox"]):
                        if "gdpr" in content_lower:
                            security_context["compliance_requirements"].append("GDPR")
                        if "hipaa" in content_lower:
                            security_context["compliance_requirements"].append("HIPAA")
                        if "pci" in content_lower:
                            security_context["compliance_requirements"].append("PCI-DSS")
                    
                    # Identify previous security issues
                    if any(term in content_lower for term in ["breach", "leak", "exposure", "vulnerability"]):
                        security_context["previous_security_issues"].append("data_exposure_concern")
                    
                    # Assess data sensitivity based on domain
                    if any(term in content_lower for term in ["financial", "health", "personal"]):
                        security_context["data_sensitivity"] = "high"
                    elif any(term in content_lower for term in ["public", "marketing", "general"]):
                        security_context["data_sensitivity"] = "low"
        
        # Extract from additional context
        if additional_context:
            security_context.update({
                key: value for key, value in additional_context.items()
                if key in ["data_sensitivity", "compliance_requirements", "risk_tolerance"]
            })
        
        return security_context
    
    async def _perform_enhanced_detection(
        self,
        content: str,
        security_context: Dict[str, Any]
    ) -> List[SecurityFinding]:
        """Perform enhanced pattern-based security detection"""
        findings = []
        
        for category, compiled_patterns in self._compiled_patterns.items():
            for pattern, base_confidence, description in compiled_patterns:
                matches = pattern.finditer(content)
                
                for match in matches:
                    # Adjust confidence based on context
                    adjusted_confidence = self._adjust_confidence_for_context(
                        base_confidence, category, security_context
                    )
                    
                    # Skip low-confidence matches unless in high-security context
                    if adjusted_confidence < 0.3 and security_context.get("data_sensitivity") != "high":
                        continue
                    
                    finding = SecurityFinding(
                        finding_id=f"{category.value}_{len(findings)}_{match.start()}",
                        category=category,
                        risk_level=SecurityRiskLevel.MEDIUM,  # Will be assessed later
                        pattern_matched=pattern.pattern,
                        context_snippet=self._extract_context_snippet(content, match.start(), match.end()),
                        confidence=adjusted_confidence,
                        position=(match.start(), match.end()),
                        remediation=self._get_remediation_advice(category, description),
                        learned_pattern=False
                    )
                    findings.append(finding)
        
        return findings
    
    async def _apply_learned_security_patterns(
        self,
        content: str,
        security_context: Dict[str, Any]
    ) -> List[SecurityFinding]:
        """Apply learned security patterns from the pattern learner"""
        findings = []
        
        try:
            # Apply learned security patterns
            pattern_results = await self._pattern_learner.apply_patterns(
                content=content,
                pattern_type=PatternType.SECURITY,
                context=security_context
            )
            
            for match in pattern_results.get("matches", []):
                finding = SecurityFinding(
                    finding_id=f"learned_{match['pattern_id']}_{match['start_pos']}",
                    category=self._infer_category_from_pattern(match),
                    risk_level=SecurityRiskLevel.MEDIUM,  # Will be assessed later
                    pattern_matched=match.get("description", "Learned pattern"),
                    context_snippet=match["match_text"],
                    confidence=match["confidence"],
                    position=(match["start_pos"], match["end_pos"]),
                    remediation=self._get_learned_pattern_remediation(match),
                    learned_pattern=True
                )
                findings.append(finding)
                
        except Exception as e:
            self.logger.warning(f"Failed to apply learned security patterns: {e}")
        
        return findings
    
    def _assess_risk_levels(
        self,
        findings: List[SecurityFinding],
        security_context: Dict[str, Any]
    ) -> List[SecurityFinding]:
        """Assess risk levels for findings based on context"""
        assessed_findings = []
        
        for finding in findings:
            # Base risk assessment by category
            base_risk = self._get_base_risk_for_category(finding.category)
            
            # Adjust risk based on context
            adjusted_risk = self._adjust_risk_for_context(
                base_risk, finding, security_context
            )
            
            # Update finding with assessed risk
            finding.risk_level = adjusted_risk
            assessed_findings.append(finding)
        
        return assessed_findings
    
    def _get_base_risk_for_category(self, category: PIICategory) -> SecurityRiskLevel:
        """Get base risk level for a PII category"""
        base_risks = {
            PIICategory.PERSONAL_IDENTIFIER: SecurityRiskLevel.HIGH,
            PIICategory.FINANCIAL: SecurityRiskLevel.CRITICAL,
            PIICategory.HEALTH: SecurityRiskLevel.CRITICAL,
            PIICategory.TECHNICAL: SecurityRiskLevel.MEDIUM,
            PIICategory.CREDENTIAL: SecurityRiskLevel.CRITICAL,
            PIICategory.BIOMETRIC: SecurityRiskLevel.CRITICAL,
            PIICategory.LOCATION: SecurityRiskLevel.MEDIUM,
        }
        return base_risks.get(category, SecurityRiskLevel.MEDIUM)
    
    def _adjust_risk_for_context(
        self,
        base_risk: SecurityRiskLevel,
        finding: SecurityFinding,
        security_context: Dict[str, Any]
    ) -> SecurityRiskLevel:
        """Adjust risk level based on security context"""
        risk_score = self._risk_scores[base_risk]
        
        # Adjust based on data sensitivity
        data_sensitivity = security_context.get("data_sensitivity", "medium")
        if data_sensitivity == "high":
            risk_score += 1
        elif data_sensitivity == "low":
            risk_score -= 1
        
        # Adjust based on compliance requirements
        compliance_reqs = security_context.get("compliance_requirements", [])
        if compliance_reqs:
            # GDPR, HIPAA, PCI-DSS increase risk for relevant categories
            if finding.category in [PIICategory.PERSONAL_IDENTIFIER, PIICategory.FINANCIAL, PIICategory.HEALTH]:
                risk_score += 1
        
        # Adjust based on user expertise
        user_expertise = security_context.get("user_expertise", "unknown")
        if user_expertise == "beginner":
            risk_score += 1  # Higher risk for less experienced users
        elif user_expertise == "expert":
            risk_score -= 1  # Lower risk for experts who may handle data appropriately
        
        # Adjust based on confidence
        if finding.confidence < 0.5:
            risk_score -= 1
        elif finding.confidence > 0.9:
            risk_score += 1
        
        # Clamp to valid range
        risk_score = max(0, min(4, risk_score))
        
        # Convert back to enum
        for level, score in self._risk_scores.items():
            if score == risk_score:
                return level
        
        return base_risk  # Fallback
    
    def _adjust_confidence_for_context(
        self,
        base_confidence: float,
        category: PIICategory,
        security_context: Dict[str, Any]
    ) -> float:
        """Adjust pattern confidence based on security context"""
        adjusted = base_confidence
        
        # Boost confidence for high-sensitivity data
        if security_context.get("data_sensitivity") == "high":
            adjusted *= 1.2
        
        # Boost confidence for compliance-relevant categories
        compliance_reqs = security_context.get("compliance_requirements", [])
        if compliance_reqs and category in [PIICategory.PERSONAL_IDENTIFIER, PIICategory.FINANCIAL, PIICategory.HEALTH]:
            adjusted *= 1.1
        
        # Domain-specific adjustments
        domain_focus = security_context.get("domain_focus", [])
        if "financial" in domain_focus and category == PIICategory.FINANCIAL:
            adjusted *= 1.15
        elif "healthcare" in domain_focus and category == PIICategory.HEALTH:
            adjusted *= 1.15
        elif "technology" in domain_focus and category == PIICategory.TECHNICAL:
            adjusted *= 1.1
        
        return min(1.0, adjusted)
    
    def _calculate_overall_risk(self, findings: List[SecurityFinding]) -> SecurityRiskLevel:
        """Calculate overall risk level from individual findings"""
        if not findings:
            return SecurityRiskLevel.NONE
        
        # Weight findings by confidence and count
        risk_scores = []
        for finding in findings:
            weighted_score = self._risk_scores[finding.risk_level] * finding.confidence
            risk_scores.append(weighted_score)
        
        if not risk_scores:
            return SecurityRiskLevel.NONE
        
        # Calculate weighted average
        avg_score = sum(risk_scores) / len(risk_scores)
        
        # Add bonus for multiple high-risk findings
        critical_count = sum(1 for f in findings if f.risk_level == SecurityRiskLevel.CRITICAL)
        high_count = sum(1 for f in findings if f.risk_level == SecurityRiskLevel.HIGH)
        
        if critical_count > 0:
            avg_score += min(1.0, critical_count * 0.5)
        if high_count > 2:
            avg_score += min(0.5, (high_count - 2) * 0.2)
        
        # Convert to risk level
        if avg_score >= 3.5:
            return SecurityRiskLevel.CRITICAL
        elif avg_score >= 2.5:
            return SecurityRiskLevel.HIGH
        elif avg_score >= 1.5:
            return SecurityRiskLevel.MEDIUM
        elif avg_score >= 0.5:
            return SecurityRiskLevel.LOW
        else:
            return SecurityRiskLevel.NONE
    
    def _generate_enhanced_sanitized_content(
        self,
        content: str,
        findings: List[SecurityFinding]
    ) -> str:
        """Generate sanitized content based on findings"""
        sanitized = content
        
        # Sort findings by position (reverse order to maintain positions)
        sorted_findings = sorted(findings, key=lambda f: f.position[0], reverse=True)
        
        for finding in sorted_findings:
            start, end = finding.position
            # Replace with appropriate redaction based on category
            redaction = self._get_redaction_text(finding.category, finding.risk_level)
            sanitized = sanitized[:start] + redaction + sanitized[end:]
        
        return sanitized
    
    def _get_redaction_text(self, category: PIICategory, risk_level: SecurityRiskLevel) -> str:
        """Get appropriate redaction text for category and risk level"""
        redactions = {
            PIICategory.PERSONAL_IDENTIFIER: "[PII_REDACTED]",
            PIICategory.FINANCIAL: "[FINANCIAL_DATA_REDACTED]",
            PIICategory.HEALTH: "[HEALTH_DATA_REDACTED]",
            PIICategory.TECHNICAL: "[TECHNICAL_DATA_REDACTED]",
            PIICategory.CREDENTIAL: "[CREDENTIAL_REDACTED]",
            PIICategory.BIOMETRIC: "[BIOMETRIC_DATA_REDACTED]",
            PIICategory.LOCATION: "[LOCATION_REDACTED]",
        }
        
        base_redaction = redactions.get(category, "[SENSITIVE_DATA_REDACTED]")
        
        # Add risk indicator for high-risk items
        if risk_level == SecurityRiskLevel.CRITICAL:
            return f"[CRITICAL_{base_redaction[1:-1]}]"
        elif risk_level == SecurityRiskLevel.HIGH:
            return f"[HIGH_RISK_{base_redaction[1:-1]}]"
        
        return base_redaction
    
    def _generate_security_recommendations(
        self,
        findings: List[SecurityFinding],
        security_context: Dict[str, Any],
        memory_context: Optional[ConversationContext]
    ) -> List[str]:
        """Generate security recommendations based on findings and context"""
        recommendations = []
        
        if not findings:
            recommendations.append("No security issues detected in the provided content.")
            return recommendations
        
        # Risk-level based recommendations
        critical_findings = [f for f in findings if f.risk_level == SecurityRiskLevel.CRITICAL]
        high_findings = [f for f in findings if f.risk_level == SecurityRiskLevel.HIGH]
        
        if critical_findings:
            recommendations.append(
                f"CRITICAL: {len(critical_findings)} critical security issue(s) detected. "
                "Immediate remediation required before sharing or processing this data."
            )
        
        if high_findings:
            recommendations.append(
                f"HIGH RISK: {len(high_findings)} high-risk security issue(s) detected. "
                "Review and sanitize before further processing."
            )
        
        # Category-specific recommendations
        categories_found = set(f.category for f in findings)
        
        if PIICategory.CREDENTIAL in categories_found:
            recommendations.append(
                "Credentials detected: Rotate any exposed credentials immediately and "
                "implement secure credential management practices."
            )
        
        if PIICategory.FINANCIAL in categories_found:
            recommendations.append(
                "Financial data detected: Ensure compliance with PCI-DSS requirements "
                "and implement appropriate data protection measures."
            )
        
        if PIICategory.HEALTH in categories_found:
            recommendations.append(
                "Health data detected: Ensure compliance with HIPAA and other relevant "
                "healthcare data protection regulations."
            )
        
        # Context-aware recommendations
        compliance_reqs = security_context.get("compliance_requirements", [])
        if "GDPR" in compliance_reqs:
            recommendations.append(
                "GDPR compliance: Ensure data subject consent and implement appropriate "
                "technical and organizational measures for data protection."
            )
        
        user_expertise = security_context.get("user_expertise", "unknown")
        if user_expertise == "beginner":
            recommendations.append(
                "Consider consulting with security experts for proper data handling "
                "and protection procedures."
            )
        
        # Pattern learning recommendations
        learned_findings = [f for f in findings if f.learned_pattern]
        if learned_findings:
            recommendations.append(
                f"Pattern learning active: {len(learned_findings)} finding(s) detected "
                "using learned patterns. Provide feedback to improve detection accuracy."
            )
        
        return recommendations
    
    def _extract_context_snippet(self, content: str, start: int, end: int, context_size: int = 20) -> str:
        """Extract context snippet around a finding"""
        snippet_start = max(0, start - context_size)
        snippet_end = min(len(content), end + context_size)
        snippet = content[snippet_start:snippet_end]
        
        # Mask the actual finding for privacy
        relative_start = start - snippet_start
        relative_end = end - snippet_start
        masked_snippet = (
            snippet[:relative_start] + 
            "[REDACTED]" + 
            snippet[relative_end:]
        )
        
        return masked_snippet
    
    def _get_remediation_advice(self, category: PIICategory, description: str) -> str:
        """Get remediation advice for a security finding"""
        remediation_map = {
            PIICategory.PERSONAL_IDENTIFIER: "Remove or mask personal identifiers before sharing",
            PIICategory.FINANCIAL: "Redact financial information and ensure PCI compliance",
            PIICategory.HEALTH: "Remove health data and ensure HIPAA compliance",
            PIICategory.TECHNICAL: "Review if technical details should be shared externally",
            PIICategory.CREDENTIAL: "Remove credentials and rotate if exposed",
            PIICategory.BIOMETRIC: "Remove biometric data immediately",
            PIICategory.LOCATION: "Consider if location data is necessary to share",
        }
        
        base_advice = remediation_map.get(category, "Review and redact if sensitive")
        return f"{base_advice}: {description}"
    
    def _get_learned_pattern_remediation(self, match: Dict[str, Any]) -> str:
        """Get remediation advice for learned pattern matches"""
        return f"Learned pattern detected: {match.get('description', 'Review for sensitivity')}"
    
    def _infer_category_from_pattern(self, match: Dict[str, Any]) -> PIICategory:
        """Infer PII category from learned pattern match"""
        description = match.get("description", "").lower()
        
        if any(term in description for term in ["credential", "password", "token", "key"]):
            return PIICategory.CREDENTIAL
        elif any(term in description for term in ["email", "phone", "ssn", "personal"]):
            return PIICategory.PERSONAL_IDENTIFIER
        elif any(term in description for term in ["financial", "credit", "bank"]):
            return PIICategory.FINANCIAL
        elif any(term in description for term in ["health", "medical", "hipaa"]):
            return PIICategory.HEALTH
        elif any(term in description for term in ["ip", "mac", "uuid", "technical"]):
            return PIICategory.TECHNICAL
        elif any(term in description for term in ["location", "address", "gps"]):
            return PIICategory.LOCATION
        else:
            return PIICategory.TECHNICAL  # Default category
    
    def _group_findings_by_category(self, findings: List[SecurityFinding]) -> Dict[PIICategory, int]:
        """Group findings by PII category"""
        groups = {}
        for finding in findings:
            groups[finding.category] = groups.get(finding.category, 0) + 1
        return groups
    
    def _group_findings_by_risk(self, findings: List[SecurityFinding]) -> Dict[SecurityRiskLevel, int]:
        """Group findings by risk level"""
        groups = {}
        for finding in findings:
            groups[finding.risk_level] = groups.get(finding.risk_level, 0) + 1
        return groups
    
    def _get_applied_patterns(self, findings: List[SecurityFinding]) -> List[str]:
        """Get list of patterns that were applied"""
        patterns = []
        for finding in findings:
            if finding.learned_pattern:
                patterns.append(f"learned_pattern_{finding.category.value}")
            else:
                patterns.append(f"builtin_pattern_{finding.category.value}")
        return list(set(patterns))  # Remove duplicates
    
    def _extract_security_feedback(
        self,
        user_feedback: Dict[str, Any],
        assessment_result: SecurityAssessmentResult
    ) -> Dict[str, Any]:
        """Extract security-specific feedback for learning"""
        security_feedback = {
            "false_positives": [],
            "missed_detections": [],
            "risk_level_corrections": [],
            "category_corrections": []
        }
        
        # Extract feedback about false positives
        if "false_positives" in user_feedback:
            security_feedback["false_positives"] = user_feedback["false_positives"]
        
        # Extract feedback about missed detections
        if "missed_detections" in user_feedback:
            security_feedback["missed_detections"] = user_feedback["missed_detections"]
        
        # Extract risk level corrections
        if "risk_corrections" in user_feedback:
            security_feedback["risk_level_corrections"] = user_feedback["risk_corrections"]
        
        return security_feedback
    
    def _update_security_metrics(self, result: SecurityAssessmentResult):
        """Update security assessment metrics"""
        self._security_metrics["assessments_performed"] += 1
        
        if result.memory_enhanced:
            self._security_metrics["memory_enhanced_assessments"] += 1
        
        self._security_metrics["total_findings"] += result.total_findings
        
        high_risk_count = result.findings_by_risk.get(SecurityRiskLevel.HIGH, 0)
        critical_count = result.findings_by_risk.get(SecurityRiskLevel.CRITICAL, 0)
        self._security_metrics["high_risk_findings"] += high_risk_count + critical_count
        
        # Update running averages
        count = self._security_metrics["assessments_performed"]
        
        current_avg_time = self._security_metrics["avg_assessment_time"]
        self._security_metrics["avg_assessment_time"] = (
            (current_avg_time * (count - 1) + result.processing_time_ms) / count
        )
        
        risk_score = self._risk_scores.get(result.overall_risk_level, 0)
        current_avg_risk = self._security_metrics["avg_risk_level"]
        self._security_metrics["avg_risk_level"] = (
            (current_avg_risk * (count - 1) + risk_score) / count
        )
    
    def _generate_assessment_id(self, content: str) -> str:
        """Generate unique assessment ID"""
        timestamp = str(int(time.time() * 1000))
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:8]
        return f"sec_assess_{timestamp}_{content_hash}"
    
    def _hash_content(self, content: str) -> str:
        """Generate hash for content"""
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    
    def get_security_statistics(self) -> Dict[str, Any]:
        """Get comprehensive security assessment statistics"""
        return {
            "metrics": self._security_metrics.copy(),
            "assessment_history_count": len(self._assessment_history),
            "pattern_categories": list(self._enhanced_security_patterns.keys()),
            "total_patterns": sum(
                len(patterns) for patterns in self._enhanced_security_patterns.values()
            )
        }