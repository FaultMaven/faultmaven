"""Pattern Learner - Phase 3

Purpose: Learning system for data patterns and user feedback integration

Requirements:
--------------------------------------------------------------------------------
• Pattern learning from user feedback and processing results
• Adaptive pattern recognition and improvement over time
• Integration with memory service for persistent learning
• Pattern quality assessment and confidence scoring
• Pattern generalization and transfer learning

Key Components:
--------------------------------------------------------------------------------
  class PatternLearner: Core pattern learning and management
  def learn_from_feedback(feedback: dict) -> bool
  def extract_patterns(content: str, classification: str) -> List[Pattern]
  def apply_patterns(content: str, patterns: List[Pattern]) -> dict

Technology Stack:
--------------------------------------------------------------------------------
scikit-learn, pandas, MemoryService, Redis

Core Design Principles:
--------------------------------------------------------------------------------
• Continuous Learning: Improve patterns from user interactions
• Quality Assessment: Score patterns based on effectiveness
• Privacy-First: No PII in learned patterns
• Resilience: Robust pattern validation and error handling
• Observability: Track learning effectiveness and pattern usage
"""

import logging
import re
import time
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from collections import defaultdict, deque
from enum import Enum
import json
import hashlib

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans
from sklearn.metrics.pairwise import cosine_similarity

from faultmaven.models.interfaces import IMemoryService
from faultmaven.infrastructure.observability.tracing import trace


class PatternType(Enum):
    """Types of patterns that can be learned"""
    CLASSIFICATION = "classification"
    ANOMALY = "anomaly"
    PERFORMANCE = "performance"
    SECURITY = "security"
    ERROR = "error"
    STRUCTURAL = "structural"


class PatternSource(Enum):
    """Sources of pattern learning"""
    USER_FEEDBACK = "user_feedback"
    AUTOMATED_ANALYSIS = "automated_analysis"
    EXPERT_ANNOTATION = "expert_annotation"
    SYSTEM_CORRELATION = "system_correlation"


@dataclass
class Pattern:
    """Individual learned pattern with metadata"""
    pattern_id: str
    pattern_type: PatternType
    pattern_source: PatternSource
    regex_pattern: str
    description: str
    confidence: float
    frequency: int
    last_seen: float
    success_rate: float
    context_tags: List[str]
    metadata: Dict[str, Any]


@dataclass
class LearningResult:
    """Result of pattern learning operation"""
    patterns_learned: int
    patterns_updated: int
    patterns_removed: int
    learning_confidence: float
    processing_time_ms: float
    errors: List[str]


class PatternLearner:
    """
    Core pattern learning system for adaptive data processing
    
    This class implements machine learning techniques to automatically
    discover and refine patterns from user feedback, processing results,
    and system observations. It supports multiple pattern types and
    provides quality assessment and pattern lifecycle management.
    """
    
    def __init__(self, memory_service: Optional[IMemoryService] = None):
        self.logger = logging.getLogger(__name__)
        self._memory_service = memory_service
        
        # Pattern storage by type
        self._patterns: Dict[PatternType, List[Pattern]] = defaultdict(list)
        self._pattern_index: Dict[str, Pattern] = {}
        
        # Learning history and metrics
        self._learning_history = deque(maxlen=1000)
        self._pattern_usage = defaultdict(int)
        self._pattern_feedback = defaultdict(list)
        
        # Performance metrics
        self._metrics = {
            "patterns_learned": 0,
            "feedback_sessions": 0,
            "pattern_applications": 0,
            "avg_pattern_confidence": 0.0,
            "learning_accuracy": 0.0,
            "pattern_diversity": 0.0
        }
        
        # Learning parameters
        self._min_confidence_threshold = 0.3
        self._max_patterns_per_type = 50
        self._pattern_decay_rate = 0.95
        self._learning_rate = 0.1
        
        # Text analysis components
        self._vectorizer = TfidfVectorizer(
            max_features=1000,
            stop_words='english',
            ngram_range=(1, 3)
        )
        self._pattern_clusters = {}
        
        # Pattern validation rules
        self._validation_rules = {
            PatternType.CLASSIFICATION: self._validate_classification_pattern,
            PatternType.ANOMALY: self._validate_anomaly_pattern,
            PatternType.SECURITY: self._validate_security_pattern,
            PatternType.ERROR: self._validate_error_pattern,
            PatternType.PERFORMANCE: self._validate_performance_pattern,
            PatternType.STRUCTURAL: self._validate_structural_pattern
        }
    
    @trace("pattern_learner_learn_from_feedback")
    async def learn_from_feedback(
        self,
        content: str,
        predicted_result: Dict[str, Any],
        actual_result: Dict[str, Any],
        user_feedback: Dict[str, Any],
        session_id: str,
        context: Optional[Dict[str, Any]] = None
    ) -> LearningResult:
        """
        Learn patterns from user feedback and correction
        
        Args:
            content: Original content that was processed
            predicted_result: What the system predicted/classified
            actual_result: Correct result provided by user
            user_feedback: Additional feedback from user
            session_id: Session identifier for context
            context: Additional context information
            
        Returns:
            LearningResult with learning outcome details
        """
        start_time = time.time()
        errors = []
        patterns_learned = 0
        patterns_updated = 0
        patterns_removed = 0
        
        try:
            # Extract learning context from memory if available
            learning_context = await self._extract_learning_context(session_id, context)
            
            # Analyze feedback for different pattern types
            feedback_analysis = self._analyze_feedback(
                content, predicted_result, actual_result, user_feedback
            )
            
            # Learn classification patterns
            if feedback_analysis.get("classification_correction"):
                classification_result = await self._learn_classification_patterns(
                    content, feedback_analysis, learning_context
                )
                patterns_learned += classification_result["learned"]
                patterns_updated += classification_result["updated"]
            
            # Learn error patterns
            if feedback_analysis.get("error_patterns"):
                error_result = await self._learn_error_patterns(
                    content, feedback_analysis, learning_context
                )
                patterns_learned += error_result["learned"]
                patterns_updated += error_result["updated"]
            
            # Learn anomaly patterns
            if feedback_analysis.get("anomaly_feedback"):
                anomaly_result = await self._learn_anomaly_patterns(
                    content, feedback_analysis, learning_context
                )
                patterns_learned += anomaly_result["learned"]
                patterns_updated += anomaly_result["updated"]
            
            # Learn security patterns
            if feedback_analysis.get("security_issues"):
                security_result = await self._learn_security_patterns(
                    content, feedback_analysis, learning_context
                )
                patterns_learned += security_result["learned"]
                patterns_updated += security_result["updated"]
            
            # Cleanup low-quality patterns
            cleanup_result = self._cleanup_patterns()
            patterns_removed = cleanup_result["removed"]
            
            # Update metrics
            self._metrics["feedback_sessions"] += 1
            self._metrics["patterns_learned"] += patterns_learned
            self._update_pattern_quality_metrics()
            
            # Store learning session
            learning_session = {
                "session_id": session_id,
                "content_hash": self._hash_content(content),
                "feedback_analysis": feedback_analysis,
                "patterns_learned": patterns_learned,
                "patterns_updated": patterns_updated,
                "timestamp": time.time()
            }
            self._learning_history.append(learning_session)
            
            # Persist patterns to memory service if available
            if self._memory_service:
                await self._persist_patterns_to_memory(session_id)
            
            processing_time = (time.time() - start_time) * 1000
            learning_confidence = self._calculate_learning_confidence(
                patterns_learned, patterns_updated, feedback_analysis
            )
            
            self.logger.info(
                f"Pattern learning completed: {patterns_learned} learned, "
                f"{patterns_updated} updated, {patterns_removed} removed"
            )
            
            return LearningResult(
                patterns_learned=patterns_learned,
                patterns_updated=patterns_updated,
                patterns_removed=patterns_removed,
                learning_confidence=learning_confidence,
                processing_time_ms=processing_time,
                errors=errors
            )
            
        except Exception as e:
            self.logger.error(f"Pattern learning failed: {e}")
            errors.append(str(e))
            return LearningResult(
                patterns_learned=0,
                patterns_updated=0,
                patterns_removed=0,
                learning_confidence=0.0,
                processing_time_ms=(time.time() - start_time) * 1000,
                errors=errors
            )
    
    @trace("pattern_learner_apply_patterns")
    async def apply_patterns(
        self,
        content: str,
        pattern_type: PatternType,
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Apply learned patterns to content for enhanced processing
        
        Args:
            content: Content to analyze with patterns
            pattern_type: Type of patterns to apply
            context: Additional context for pattern application
            
        Returns:
            Dictionary with pattern application results
        """
        try:
            patterns = self._patterns.get(pattern_type, [])
            if not patterns:
                return {"matches": [], "confidence": 0.0, "patterns_applied": 0}
            
            # Filter patterns by context relevance
            relevant_patterns = self._filter_patterns_by_context(patterns, context)
            
            # Apply patterns and collect matches
            matches = []
            total_confidence = 0.0
            patterns_applied = 0
            
            for pattern in relevant_patterns:
                try:
                    # Apply regex pattern
                    regex_matches = re.finditer(pattern.regex_pattern, content, re.IGNORECASE)
                    
                    for match in regex_matches:
                        match_info = {
                            "pattern_id": pattern.pattern_id,
                            "pattern_type": pattern.pattern_type.value,
                            "match_text": match.group(),
                            "start_pos": match.start(),
                            "end_pos": match.end(),
                            "confidence": pattern.confidence,
                            "description": pattern.description,
                            "context_tags": pattern.context_tags
                        }
                        matches.append(match_info)
                        total_confidence += pattern.confidence
                        
                        # Update pattern usage
                        self._pattern_usage[pattern.pattern_id] += 1
                        pattern.last_seen = time.time()
                    
                    patterns_applied += 1
                    
                except re.error as e:
                    self.logger.warning(f"Invalid regex pattern {pattern.pattern_id}: {e}")
                    continue
            
            # Calculate aggregate confidence
            avg_confidence = total_confidence / len(matches) if matches else 0.0
            
            # Update metrics
            self._metrics["pattern_applications"] += 1
            
            return {
                "matches": matches,
                "confidence": avg_confidence,
                "patterns_applied": patterns_applied,
                "pattern_type": pattern_type.value
            }
            
        except Exception as e:
            self.logger.error(f"Pattern application failed: {e}")
            return {"matches": [], "confidence": 0.0, "patterns_applied": 0}
    
    def get_pattern_statistics(self) -> Dict[str, Any]:
        """
        Get comprehensive statistics about learned patterns
        
        Returns:
            Dictionary with pattern statistics and metrics
        """
        stats = {
            "total_patterns": len(self._pattern_index),
            "patterns_by_type": {},
            "top_patterns": [],
            "pattern_quality": {},
            "learning_metrics": self._metrics.copy(),
            "pattern_diversity": self._calculate_pattern_diversity()
        }
        
        # Patterns by type
        for pattern_type in PatternType:
            patterns = self._patterns.get(pattern_type, [])
            stats["patterns_by_type"][pattern_type.value] = {
                "count": len(patterns),
                "avg_confidence": np.mean([p.confidence for p in patterns]) if patterns else 0.0,
                "avg_success_rate": np.mean([p.success_rate for p in patterns]) if patterns else 0.0
            }
        
        # Top patterns by usage
        top_patterns = sorted(
            self._pattern_index.values(),
            key=lambda p: self._pattern_usage.get(p.pattern_id, 0),
            reverse=True
        )[:10]
        
        stats["top_patterns"] = [
            {
                "pattern_id": p.pattern_id,
                "description": p.description,
                "usage_count": self._pattern_usage.get(p.pattern_id, 0),
                "confidence": p.confidence,
                "success_rate": p.success_rate
            }
            for p in top_patterns
        ]
        
        # Pattern quality assessment
        stats["pattern_quality"] = {
            "high_confidence_patterns": len([
                p for p in self._pattern_index.values() if p.confidence > 0.8
            ]),
            "low_confidence_patterns": len([
                p for p in self._pattern_index.values() if p.confidence < 0.3
            ]),
            "recently_used_patterns": len([
                p for p in self._pattern_index.values() 
                if time.time() - p.last_seen < 86400  # 24 hours
            ])
        }
        
        return stats
    
    async def _extract_learning_context(
        self,
        session_id: str,
        context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Extract learning context from memory and provided context"""
        learning_context = {
            "user_expertise": "unknown",
            "domain_focus": [],
            "recent_patterns": [],
            "session_context": context or {}
        }
        
        if self._memory_service:
            try:
                # Get conversation context for learning enhancement
                conv_context = await self._memory_service.retrieve_context(
                    session_id, "pattern learning context"
                )
                
                if conv_context and conv_context.user_profile:
                    learning_context["user_expertise"] = conv_context.user_profile.get(
                        "skill_level", "unknown"
                    )
                
                if conv_context and conv_context.domain_context:
                    learning_context["domain_focus"] = list(conv_context.domain_context.keys())
                    
            except Exception as e:
                self.logger.warning(f"Failed to extract learning context: {e}")
        
        return learning_context
    
    def _analyze_feedback(
        self,
        content: str,
        predicted_result: Dict[str, Any],
        actual_result: Dict[str, Any],
        user_feedback: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Analyze feedback to determine learning opportunities"""
        analysis = {
            "classification_correction": False,
            "error_patterns": [],
            "anomaly_feedback": {},
            "security_issues": [],
            "performance_feedback": {},
            "quality_feedback": {}
        }
        
        # Check for classification corrections
        predicted_type = predicted_result.get("data_type") or predicted_result.get("classification")
        actual_type = actual_result.get("data_type") or actual_result.get("classification")
        
        if predicted_type != actual_type:
            analysis["classification_correction"] = {
                "predicted": predicted_type,
                "actual": actual_type,
                "confidence_gap": predicted_result.get("confidence", 0.5)
            }
        
        # Extract error patterns from feedback
        if "errors" in user_feedback or "error_patterns" in user_feedback:
            analysis["error_patterns"] = user_feedback.get("error_patterns", [])
        
        # Anomaly feedback
        if "anomalies" in user_feedback:
            analysis["anomaly_feedback"] = user_feedback["anomalies"]
        
        # Security issue feedback
        if "security_issues" in user_feedback:
            analysis["security_issues"] = user_feedback["security_issues"]
        
        # Performance feedback
        if "performance" in user_feedback:
            analysis["performance_feedback"] = user_feedback["performance"]
        
        # Quality feedback
        if "quality_rating" in user_feedback:
            analysis["quality_feedback"] = {
                "rating": user_feedback["quality_rating"],
                "comments": user_feedback.get("comments", "")
            }
        
        return analysis
    
    async def _learn_classification_patterns(
        self,
        content: str,
        feedback_analysis: Dict[str, Any],
        learning_context: Dict[str, Any]
    ) -> Dict[str, int]:
        """Learn patterns for improved classification"""
        learned = 0
        updated = 0
        
        correction = feedback_analysis["classification_correction"]
        actual_type = correction["actual"]
        
        # Extract distinctive patterns from correctly classified content
        extracted_patterns = self._extract_classification_patterns(content, actual_type)
        
        for pattern_info in extracted_patterns:
            pattern_id = self._generate_pattern_id(pattern_info["regex"], PatternType.CLASSIFICATION)
            
            if pattern_id in self._pattern_index:
                # Update existing pattern
                existing_pattern = self._pattern_index[pattern_id]
                existing_pattern.frequency += 1
                existing_pattern.confidence = min(1.0, existing_pattern.confidence + self._learning_rate)
                updated += 1
            else:
                # Create new pattern
                new_pattern = Pattern(
                    pattern_id=pattern_id,
                    pattern_type=PatternType.CLASSIFICATION,
                    pattern_source=PatternSource.USER_FEEDBACK,
                    regex_pattern=pattern_info["regex"],
                    description=pattern_info["description"],
                    confidence=0.6,  # Start with moderate confidence
                    frequency=1,
                    last_seen=time.time(),
                    success_rate=1.0,
                    context_tags=[actual_type, learning_context.get("user_expertise", "unknown")],
                    metadata={"target_classification": actual_type}
                )
                
                if self._validate_pattern(new_pattern):
                    self._add_pattern(new_pattern)
                    learned += 1
        
        return {"learned": learned, "updated": updated}
    
    async def _learn_error_patterns(
        self,
        content: str,
        feedback_analysis: Dict[str, Any],
        learning_context: Dict[str, Any]
    ) -> Dict[str, int]:
        """Learn patterns for error detection and classification"""
        learned = 0
        updated = 0
        
        error_patterns = feedback_analysis["error_patterns"]
        
        for error_info in error_patterns:
            # Extract error-specific patterns
            extracted = self._extract_error_patterns(content, error_info)
            
            for pattern_info in extracted:
                pattern_id = self._generate_pattern_id(pattern_info["regex"], PatternType.ERROR)
                
                if pattern_id in self._pattern_index:
                    existing_pattern = self._pattern_index[pattern_id]
                    existing_pattern.frequency += 1
                    existing_pattern.confidence = min(1.0, existing_pattern.confidence + self._learning_rate)
                    updated += 1
                else:
                    new_pattern = Pattern(
                        pattern_id=pattern_id,
                        pattern_type=PatternType.ERROR,
                        pattern_source=PatternSource.USER_FEEDBACK,
                        regex_pattern=pattern_info["regex"],
                        description=pattern_info["description"],
                        confidence=0.7,
                        frequency=1,
                        last_seen=time.time(),
                        success_rate=1.0,
                        context_tags=["error", error_info.get("error_type", "unknown")],
                        metadata={"error_info": error_info}
                    )
                    
                    if self._validate_pattern(new_pattern):
                        self._add_pattern(new_pattern)
                        learned += 1
        
        return {"learned": learned, "updated": updated}
    
    async def _learn_anomaly_patterns(
        self,
        content: str,
        feedback_analysis: Dict[str, Any],
        learning_context: Dict[str, Any]
    ) -> Dict[str, int]:
        """Learn patterns for anomaly detection"""
        learned = 0
        updated = 0
        
        # This would be enhanced with actual anomaly learning logic
        # For now, return placeholder
        return {"learned": learned, "updated": updated}
    
    async def _learn_security_patterns(
        self,
        content: str,
        feedback_analysis: Dict[str, Any],
        learning_context: Dict[str, Any]
    ) -> Dict[str, int]:
        """Learn patterns for security issue detection"""
        learned = 0
        updated = 0
        
        security_issues = feedback_analysis["security_issues"]
        
        for issue in security_issues:
            # Extract security-specific patterns
            extracted = self._extract_security_patterns(content, issue)
            
            for pattern_info in extracted:
                pattern_id = self._generate_pattern_id(pattern_info["regex"], PatternType.SECURITY)
                
                if pattern_id in self._pattern_index:
                    existing_pattern = self._pattern_index[pattern_id]
                    existing_pattern.frequency += 1
                    existing_pattern.confidence = min(1.0, existing_pattern.confidence + self._learning_rate)
                    updated += 1
                else:
                    new_pattern = Pattern(
                        pattern_id=pattern_id,
                        pattern_type=PatternType.SECURITY,
                        pattern_source=PatternSource.USER_FEEDBACK,
                        regex_pattern=pattern_info["regex"],
                        description=pattern_info["description"],
                        confidence=0.8,  # High confidence for security patterns
                        frequency=1,
                        last_seen=time.time(),
                        success_rate=1.0,
                        context_tags=["security", issue.get("issue_type", "unknown")],
                        metadata={"security_issue": issue}
                    )
                    
                    if self._validate_pattern(new_pattern):
                        self._add_pattern(new_pattern)
                        learned += 1
        
        return {"learned": learned, "updated": updated}
    
    def _extract_classification_patterns(self, content: str, classification: str) -> List[Dict[str, Any]]:
        """Extract patterns indicative of a specific classification"""
        patterns = []
        
        # Extract common classification indicators
        lines = content.split('\n')[:10]  # First 10 lines
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Look for distinctive formatting patterns
            if classification == "log_file":
                # Extract timestamp patterns
                timestamp_match = re.search(r'\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}', line)
                if timestamp_match:
                    patterns.append({
                        "regex": re.escape(timestamp_match.group()[:10]),  # Date part
                        "description": f"Log timestamp pattern for {classification}",
                        "confidence": 0.8
                    })
                
                # Extract log level patterns
                level_match = re.search(r'\b(ERROR|WARN|WARNING|INFO|DEBUG)\b', line, re.IGNORECASE)
                if level_match:
                    patterns.append({
                        "regex": rf'\b{re.escape(level_match.group().upper())}\b',
                        "description": f"Log level pattern for {classification}",
                        "confidence": 0.7
                    })
            
            elif classification == "error_message":
                # Extract error keywords
                error_words = re.findall(r'\b(error|exception|failed|failure)\b', line, re.IGNORECASE)
                for word in error_words:
                    patterns.append({
                        "regex": rf'\b{re.escape(word)}\b',
                        "description": f"Error keyword pattern for {classification}",
                        "confidence": 0.6
                    })
            
            elif classification == "config_file":
                # Extract configuration patterns
                key_value_match = re.search(r'^(\w+):\s*(.+)$', line)
                if key_value_match:
                    key = key_value_match.group(1)
                    patterns.append({
                        "regex": rf'^{re.escape(key)}:\s*',
                        "description": f"Config key pattern for {classification}",
                        "confidence": 0.7
                    })
        
        return patterns[:5]  # Limit to 5 patterns
    
    def _extract_error_patterns(self, content: str, error_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract patterns for error detection"""
        patterns = []
        
        error_type = error_info.get("error_type", "unknown")
        error_description = error_info.get("description", "")
        
        # Extract error signatures
        lines = content.split('\n')
        for line in lines:
            if any(keyword in line.lower() for keyword in ["error", "exception", "failed"]):
                # Extract meaningful words from error line
                words = re.findall(r'\b\w{4,}\b', line)
                if len(words) >= 2:
                    # Create pattern from first few words
                    pattern_words = words[:3]
                    regex_pattern = r'\b' + r'\b.*\b'.join(re.escape(w) for w in pattern_words) + r'\b'
                    
                    patterns.append({
                        "regex": regex_pattern,
                        "description": f"Error pattern: {error_type}",
                        "confidence": 0.6
                    })
        
        return patterns[:3]  # Limit to 3 patterns
    
    def _extract_security_patterns(self, content: str, issue: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract patterns for security issue detection"""
        patterns = []
        
        issue_type = issue.get("issue_type", "unknown")
        
        if issue_type == "pii_detected":
            # This would extract patterns around PII detection
            # But we need to be careful not to store actual PII
            patterns.append({
                "regex": r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
                "description": "Email pattern for PII detection",
                "confidence": 0.9
            })
        
        elif issue_type == "credential_exposure":
            patterns.append({
                "regex": r'(?:password|passwd|pwd)[:\s=]+[^\s]+',
                "description": "Password exposure pattern",
                "confidence": 0.85
            })
        
        return patterns
    
    def _validate_pattern(self, pattern: Pattern) -> bool:
        """Validate a pattern before adding it to the system"""
        try:
            # Check if it's a valid regex
            re.compile(pattern.regex_pattern)
            
            # Use type-specific validation
            validator = self._validation_rules.get(pattern.pattern_type)
            if validator:
                return validator(pattern)
            
            return True
            
        except re.error:
            return False
    
    def _validate_classification_pattern(self, pattern: Pattern) -> bool:
        """Validate classification-specific patterns"""
        # Ensure pattern is not too generic
        if len(pattern.regex_pattern) < 3:
            return False
        
        # Ensure pattern has reasonable specificity
        if pattern.regex_pattern in ['.*', '.+', '\\w+']:
            return False
        
        return True
    
    def _validate_anomaly_pattern(self, pattern: Pattern) -> bool:
        """Validate anomaly detection patterns"""
        return True  # Placeholder
    
    def _validate_security_pattern(self, pattern: Pattern) -> bool:
        """Validate security-related patterns"""
        # Security patterns should have high specificity
        return len(pattern.regex_pattern) > 10
    
    def _validate_error_pattern(self, pattern: Pattern) -> bool:
        """Validate error detection patterns"""
        return True  # Placeholder
    
    def _validate_performance_pattern(self, pattern: Pattern) -> bool:
        """Validate performance-related patterns"""
        return True  # Placeholder
    
    def _validate_structural_pattern(self, pattern: Pattern) -> bool:
        """Validate structural patterns"""
        return True  # Placeholder
    
    def _add_pattern(self, pattern: Pattern):
        """Add a validated pattern to the system"""
        self._patterns[pattern.pattern_type].append(pattern)
        self._pattern_index[pattern.pattern_id] = pattern
        
        # Limit patterns per type
        if len(self._patterns[pattern.pattern_type]) > self._max_patterns_per_type:
            # Remove lowest confidence pattern
            patterns_by_confidence = sorted(
                self._patterns[pattern.pattern_type],
                key=lambda p: p.confidence * p.frequency
            )
            pattern_to_remove = patterns_by_confidence[0]
            self._remove_pattern(pattern_to_remove.pattern_id)
    
    def _remove_pattern(self, pattern_id: str):
        """Remove a pattern from the system"""
        if pattern_id in self._pattern_index:
            pattern = self._pattern_index[pattern_id]
            self._patterns[pattern.pattern_type].remove(pattern)
            del self._pattern_index[pattern_id]
            if pattern_id in self._pattern_usage:
                del self._pattern_usage[pattern_id]
    
    def _cleanup_patterns(self) -> Dict[str, int]:
        """Remove low-quality patterns"""
        removed = 0
        
        patterns_to_remove = []
        for pattern in self._pattern_index.values():
            # Remove patterns with low confidence and low usage
            if (pattern.confidence < self._min_confidence_threshold and 
                self._pattern_usage.get(pattern.pattern_id, 0) < 2):
                patterns_to_remove.append(pattern.pattern_id)
            
            # Apply decay to old patterns
            elif time.time() - pattern.last_seen > 30 * 24 * 3600:  # 30 days
                pattern.confidence *= self._pattern_decay_rate
                if pattern.confidence < self._min_confidence_threshold:
                    patterns_to_remove.append(pattern.pattern_id)
        
        for pattern_id in patterns_to_remove:
            self._remove_pattern(pattern_id)
            removed += 1
        
        return {"removed": removed}
    
    def _filter_patterns_by_context(
        self,
        patterns: List[Pattern],
        context: Optional[Dict[str, Any]]
    ) -> List[Pattern]:
        """Filter patterns based on context relevance"""
        if not context:
            return patterns
        
        relevant_patterns = []
        context_tags = context.get("tags", [])
        domain_focus = context.get("domain_focus", [])
        
        for pattern in patterns:
            relevance_score = 0.0
            
            # Check tag alignment
            for tag in pattern.context_tags:
                if tag in context_tags:
                    relevance_score += 0.3
            
            # Check domain alignment
            for domain in domain_focus:
                if domain in pattern.context_tags:
                    relevance_score += 0.2
            
            # Include high-confidence patterns regardless
            if pattern.confidence > 0.8:
                relevance_score += 0.2
            
            # Include if relevance threshold met
            if relevance_score > 0.1 or pattern.confidence > 0.9:
                relevant_patterns.append(pattern)
        
        return relevant_patterns
    
    def _generate_pattern_id(self, regex_pattern: str, pattern_type: PatternType) -> str:
        """Generate unique ID for a pattern"""
        content = f"{pattern_type.value}:{regex_pattern}"
        return hashlib.md5(content.encode()).hexdigest()[:16]
    
    def _hash_content(self, content: str) -> str:
        """Generate hash for content"""
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    
    def _calculate_learning_confidence(
        self,
        patterns_learned: int,
        patterns_updated: int,
        feedback_analysis: Dict[str, Any]
    ) -> float:
        """Calculate confidence in the learning process"""
        base_confidence = 0.5
        
        # Boost based on patterns learned
        if patterns_learned > 0:
            base_confidence += min(0.3, patterns_learned * 0.1)
        
        # Boost based on patterns updated
        if patterns_updated > 0:
            base_confidence += min(0.2, patterns_updated * 0.05)
        
        # Consider feedback quality
        if feedback_analysis.get("quality_feedback", {}).get("rating", 0) > 3:
            base_confidence += 0.1
        
        return min(1.0, base_confidence)
    
    def _update_pattern_quality_metrics(self):
        """Update aggregate pattern quality metrics"""
        if not self._pattern_index:
            return
        
        confidences = [p.confidence for p in self._pattern_index.values()]
        success_rates = [p.success_rate for p in self._pattern_index.values()]
        
        self._metrics["avg_pattern_confidence"] = np.mean(confidences)
        self._metrics["learning_accuracy"] = np.mean(success_rates)
        self._metrics["pattern_diversity"] = self._calculate_pattern_diversity()
    
    def _calculate_pattern_diversity(self) -> float:
        """Calculate diversity of learned patterns"""
        if len(self._pattern_index) < 2:
            return 0.0
        
        # Calculate diversity based on pattern types and regex complexity
        type_counts = defaultdict(int)
        complexity_scores = []
        
        for pattern in self._pattern_index.values():
            type_counts[pattern.pattern_type] += 1
            complexity_scores.append(len(pattern.regex_pattern))
        
        # Type diversity (Shannon entropy)
        total_patterns = len(self._pattern_index)
        type_entropy = 0.0
        for count in type_counts.values():
            p = count / total_patterns
            if p > 0:
                type_entropy -= p * np.log2(p)
        
        # Normalize entropy
        max_entropy = np.log2(len(PatternType))
        type_diversity = type_entropy / max_entropy if max_entropy > 0 else 0.0
        
        # Complexity diversity
        complexity_std = np.std(complexity_scores) if complexity_scores else 0.0
        complexity_diversity = min(1.0, complexity_std / 20.0)  # Normalize
        
        return (type_diversity + complexity_diversity) / 2.0
    
    async def _persist_patterns_to_memory(self, session_id: str):
        """Persist learned patterns to memory service"""
        try:
            if not self._memory_service:
                return
            
            # Create a summary of patterns for memory consolidation
            pattern_summary = {
                "total_patterns": len(self._pattern_index),
                "patterns_by_type": {
                    pt.value: len(self._patterns.get(pt, []))
                    for pt in PatternType
                },
                "top_patterns": [
                    {
                        "id": p.pattern_id,
                        "type": p.pattern_type.value,
                        "description": p.description,
                        "confidence": p.confidence
                    }
                    for p in sorted(
                        self._pattern_index.values(),
                        key=lambda x: x.confidence,
                        reverse=True
                    )[:5]
                ],
                "learning_metrics": self._metrics.copy()
            }
            
            # Store pattern learning result
            await self._memory_service.consolidate_insights(session_id, {
                "pattern_learning": pattern_summary,
                "learning_timestamp": time.time()
            })
            
        except Exception as e:
            self.logger.warning(f"Failed to persist patterns to memory: {e}")