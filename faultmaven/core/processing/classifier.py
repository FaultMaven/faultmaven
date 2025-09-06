"""Enhanced Data Classifier - Phase 3

Purpose: Memory-aware data classification with context understanding and pattern learning

Requirements:
--------------------------------------------------------------------------------
• Memory-enhanced classification using conversation context
• Pattern learning from user feedback and historical data
• Context-aware classification optimization
• Enhanced security assessment for PII detection
• Integration with memory service for historical patterns

Key Components:
--------------------------------------------------------------------------------
  class EnhancedDataClassifier: Enhanced classifier with memory integration
  def classify_with_context(content: str, context: dict) -> ClassificationResult
  def learn_from_feedback(classification: ClassificationResult, feedback: dict)
  def _memory_aware_classification(content: str, memory_context: dict)

Technology Stack:
--------------------------------------------------------------------------------
PyYAML, Regex, LLMRouter, MemoryService, PatternLearner

Core Design Principles:
--------------------------------------------------------------------------------
• Memory-Aware: Use conversation context for better classification
• Learning: Continuously improve from user interactions
• Privacy-First: Enhanced PII detection and sanitization
• Resilience: Implement retries and fallbacks with memory
• Cost-Efficiency: Use semantic caching with context awareness
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import json
import logging
import re
import time
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from collections import defaultdict, deque

import yaml

from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.models.api import DataType
from faultmaven.models.interfaces import IDataClassifier, IMemoryService, ConversationContext
from faultmaven.infrastructure.observability.tracing import trace


@dataclass
class ClassificationResult:
    """Enhanced classification result with context and confidence information"""
    data_type: DataType
    confidence: float
    context_relevance: float
    pattern_matches: List[str]
    security_flags: List[str]
    learned_patterns: List[str]
    memory_enhanced: bool
    processing_time_ms: float


class EnhancedDataClassifier(IDataClassifier):
    """Memory-aware data classifier with pattern learning and context understanding"""

    def __init__(self, memory_service: Optional[IMemoryService] = None):
        self.logger = logging.getLogger(__name__)
        self.llm_router = LLMRouter()
        self._memory_service = memory_service
        
        # Pattern learning components
        self._learned_patterns = defaultdict(list)
        self._pattern_feedback = defaultdict(dict)
        self._classification_history = deque(maxlen=1000)
        self._context_patterns = defaultdict(list)
        
        # Performance metrics
        self._metrics = {
            "classifications": 0,
            "memory_enhanced": 0,
            "pattern_matches": 0,
            "learning_updates": 0,
            "avg_confidence": 0.0,
            "avg_context_relevance": 0.0
        }

        # Enhanced heuristic patterns with confidence weights
        self.weighted_patterns = {
            DataType.LOG_FILE: [
                # Timestamp patterns (high weight)
                (r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}", 3.0),
                (r"\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}", 3.0),
                # Log level indicators (high weight)
                (r"\b(ERROR|WARN|WARNING|INFO|DEBUG|FATAL|CRITICAL)\b", 2.5),
                # Common log prefixes (medium weight)
                (r"^\d{4}-\d{2}-\d{2}", 2.0),
                (r"^\[\d{4}-\d{2}-\d{2}", 2.0),
                # Log file extensions (low weight)
                (r"\.(log|txt)$", 1.0),
            ],
            DataType.ERROR_REPORT: [
                # Error keywords (high weight)
                (r"\b(error|exception|failed|failure|crash|abort)\b", 3.0),
                # Exception patterns (high weight)
                (r"Exception:", 2.5),
                (r"Error:", 2.5),
                (r"Traceback \(most recent call last\):", 3.0),
                # HTTP error codes (medium weight)
                (r"\b(4\d{2}|5\d{2})\b", 2.0),
                # Stack trace patterns (high weight for error reports)
                (r"at\s+[\w\.$<>]+\([^)]*\)", 2.8),
                (r'File "[^"]+", line \d+', 2.5),
                (r"^\s+at\s+", 2.0),
            ],
            DataType.CONFIG_FILE: [
                # YAML indicators (high weight)
                (r"^---\s*$", 3.0),
                (r"^\w+:\s*$", 2.5),
                (r"^\s+-\s+\w+:", 2.5),
                # JSON config (high weight)
                (r'\{[^{}]*"config":', 3.0),
                (r'\{[^{}]*"settings":', 3.0),
                # INI format (medium weight)
                (r"^\[[^\]]+\]", 2.5),
                (r"^\w+\s*=\s*[^=]+$", 2.0),
                # Environment variables (low weight)
                (r"^\w+=\w+$", 1.5),
            ],
            DataType.DOCUMENTATION: [
                # Markdown (high weight)
                (r"^#\s+\w+", 3.0),
                (r"^\*\*[^*]+\*\*", 2.0),
                (r"^\[[^\]]+\]\([^)]+\)", 2.0),
                # Documentation keywords (high weight)
                (r"\b(guide|manual|documentation|tutorial|help)\b", 3.0),
                # HTML tags (medium weight)
                (r"<[^>]+>", 2.0),
                # Code blocks (medium weight)
                (r"```\w*", 2.5),
            ],
            DataType.SCREENSHOT: [
                # Image file indicators (high weight)
                (r"\.(png|jpg|jpeg|gif|bmp|webp)$", 3.0),
                # Image file headers (very high weight)
                (r"^PNG\r\n", 3.5),
                (r"^\xff\xd8\xff", 3.5),  # JPEG header
                (r"^GIF8[79]a", 3.5),  # GIF header
                # Base64 image data patterns (medium weight)
                (r"data:image/[^;]+;base64,", 2.5),
                # Image metadata patterns (low weight)
                (r"\b(width|height|resolution|pixels)\b", 1.0),
            ],
            DataType.OTHER: [
                # Catch-all patterns for unstructured data
                (r"^.{1,100}$", 0.5),  # Very short content (low confidence)
                (r"^\s*$", 0.1),       # Empty or whitespace-only (very low confidence)
            ],
        }

        # Compile patterns for efficiency with weights
        self.compiled_weighted_patterns = {}
        for data_type, pattern_list in self.weighted_patterns.items():
            self.compiled_weighted_patterns[data_type] = [
                (re.compile(pattern, re.IGNORECASE | re.MULTILINE), weight)
                for pattern, weight in pattern_list
            ]

        # Security patterns for PII detection
        self.security_patterns = {
            "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
            "phone": re.compile(r"\b\d{3}-\d{3}-\d{4}\b|\b\(\d{3}\)\s*\d{3}-\d{4}\b"),
            "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
            "credit_card": re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),
            "ip_address": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
            "api_key": re.compile(r"\b[A-Za-z0-9]{32,}\b"),
            "password": re.compile(r"password[:\s=]+[^\s]+", re.IGNORECASE),
            "token": re.compile(r"token[:\s=]+[^\s]+", re.IGNORECASE)
        }

    @trace("enhanced_data_classifier_classify_with_context")
    async def classify_with_context(
        self, 
        content: str, 
        session_id: str,
        filename: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> ClassificationResult:
        """
        Memory-aware classification with conversation context integration
        
        This method leverages conversation history, user patterns, and domain
        context from the memory system to provide more accurate and relevant
        data classification.
        
        Args:
            content: Raw data content to classify
            session_id: Session identifier for memory context retrieval
            filename: Optional filename for additional classification hints
            context: Optional additional context data
            
        Returns:
            ClassificationResult with enhanced classification information
        """
        start_time = time.time()
        
        if not content or not isinstance(content, str):
            return ClassificationResult(
                data_type=DataType.OTHER,
                confidence=0.0,
                context_relevance=0.0,
                pattern_matches=[],
                security_flags=[],
                learned_patterns=[],
                memory_enhanced=False,
                processing_time_ms=0.0
            )
        
        # Retrieve memory context if available
        memory_context = None
        memory_enhanced = False
        
        if self._memory_service and session_id:
            try:
                # Create a simple query from content preview for context retrieval
                content_preview = content[:200] + "..." if len(content) > 200 else content
                memory_context = await self._memory_service.retrieve_context(session_id, content_preview)
                memory_enhanced = True
                self._metrics["memory_enhanced"] += 1
            except Exception as e:
                self.logger.warning(f"Failed to retrieve memory context for classification: {e}")
                memory_context = None
        
        # Combine context sources
        combined_context = {
            "memory": memory_context,
            "additional": context or {},
            "filename": filename,
            "session_id": session_id
        }
        
        # Perform enhanced classification
        classification_result = await self._classify_with_memory_context(
            content, combined_context, start_time
        )
        
        # Update metrics
        self._metrics["classifications"] += 1
        self._update_avg_metric("avg_confidence", classification_result.confidence)
        self._update_avg_metric("avg_context_relevance", classification_result.context_relevance)
        
        # Store classification for learning
        self._classification_history.append({
            "content_hash": hash(content) % 1000000,
            "result": classification_result,
            "context": combined_context,
            "timestamp": time.time()
        })
        
        return classification_result
    
    async def _classify_with_memory_context(
        self, 
        content: str, 
        context: Dict[str, Any], 
        start_time: float
    ) -> ClassificationResult:
        """
        Perform classification enhanced with memory context
        
        Args:
            content: Content to classify
            context: Combined context including memory and additional data
            start_time: Start time for performance measurement
            
        Returns:
            ClassificationResult with memory-enhanced classification
        """
        # Detect security issues first
        security_flags = self._detect_security_issues(content)
        
        # Get memory context insights
        memory_context = context.get("memory")
        context_insights = self._extract_context_insights(memory_context, content)
        
        # Enhanced pattern matching with context awareness
        pattern_scores = self._calculate_enhanced_pattern_scores(content, context_insights)
        
        # Apply learned patterns
        learned_pattern_scores = self._apply_learned_patterns(content, context)
        
        # Combine all scoring approaches
        combined_scores = self._combine_classification_scores(
            pattern_scores, learned_pattern_scores, context_insights
        )
        
        # Determine best classification
        if not combined_scores:
            classification = DataType.OTHER
            confidence = 0.0
            pattern_matches = []
        else:
            best_classification = max(combined_scores.items(), key=lambda x: x[1]["score"])
            classification = best_classification[0]
            confidence = min(1.0, best_classification[1]["score"] / 10.0)  # Normalize to 0-1
            pattern_matches = best_classification[1]["patterns"]
        
        # Calculate context relevance
        context_relevance = self._calculate_context_relevance(
            classification, context_insights, memory_context
        )
        
        # Get learned patterns that contributed
        learned_patterns = self._get_contributing_learned_patterns(content, classification)
        
        processing_time = (time.time() - start_time) * 1000
        
        return ClassificationResult(
            data_type=classification,
            confidence=confidence,
            context_relevance=context_relevance,
            pattern_matches=pattern_matches,
            security_flags=security_flags,
            learned_patterns=learned_patterns,
            memory_enhanced=context.get("memory") is not None,
            processing_time_ms=processing_time
        )
    
    def _detect_security_issues(self, content: str) -> List[str]:
        """
        Detect potential security and PII issues in content
        
        Args:
            content: Content to analyze for security issues
            
        Returns:
            List of detected security flags
        """
        flags = []
        
        for security_type, pattern in self.security_patterns.items():
            if pattern.search(content):
                flags.append(security_type)
        
        return flags
    
    def _extract_context_insights(
        self, 
        memory_context: Optional[ConversationContext], 
        content: str
    ) -> Dict[str, Any]:
        """
        Extract classification insights from memory context
        
        Args:
            memory_context: Retrieved conversation context
            content: Content being classified
            
        Returns:
            Dictionary of context insights for classification
        """
        insights = {
            "domain_hints": [],
            "user_expertise": "unknown",
            "conversation_themes": [],
            "previous_data_types": [],
            "technical_focus": []
        }
        
        if not memory_context:
            return insights
        
        # Extract domain hints from conversation history
        if memory_context.conversation_history:
            for item in memory_context.conversation_history[-5:]:  # Last 5 items
                content_lower = item.get("content", "").lower()
                
                # Look for technical domains
                if any(term in content_lower for term in ["kubernetes", "k8s", "docker"]):
                    insights["technical_focus"].append("containerization")
                if any(term in content_lower for term in ["database", "sql", "mysql", "postgres"]):
                    insights["technical_focus"].append("database")
                if any(term in content_lower for term in ["api", "rest", "graphql", "endpoint"]):
                    insights["technical_focus"].append("api")
                if any(term in content_lower for term in ["frontend", "react", "vue", "angular"]):
                    insights["technical_focus"].append("frontend")
                
                # Extract data type patterns from history
                if "log" in content_lower:
                    insights["previous_data_types"].append("log_file")
                if "error" in content_lower or "exception" in content_lower:
                    insights["previous_data_types"].append("error_message")
                if "config" in content_lower or "yaml" in content_lower:
                    insights["previous_data_types"].append("config_file")
        
        # Extract user expertise level
        if memory_context.user_profile:
            insights["user_expertise"] = memory_context.user_profile.get("skill_level", "unknown")
        
        # Extract domain context
        if memory_context.domain_context:
            insights["domain_hints"] = list(memory_context.domain_context.keys())
        
        return insights
    
    def _calculate_enhanced_pattern_scores(
        self, 
        content: str, 
        context_insights: Dict[str, Any]
    ) -> Dict[DataType, Dict[str, Any]]:
        """
        Calculate pattern scores enhanced with context insights
        
        Args:
            content: Content to analyze
            context_insights: Context insights for score adjustment
            
        Returns:
            Dictionary mapping DataType to score information
        """
        scores = {}
        
        for data_type, patterns in self.compiled_weighted_patterns.items():
            total_score = 0.0
            matched_patterns = []
            
            for pattern, base_weight in patterns:
                matches = pattern.findall(content)
                if matches:
                    # Apply context-aware weight adjustment
                    adjusted_weight = self._adjust_weight_for_context(
                        data_type, base_weight, context_insights
                    )
                    score_contribution = len(matches) * adjusted_weight
                    total_score += score_contribution
                    matched_patterns.extend([pattern.pattern] * len(matches))
            
            # Normalize score by content length
            if len(content) > 0:
                total_score = total_score / len(content) * 1000  # Scale for readability
            
            scores[data_type] = {
                "score": total_score,
                "patterns": matched_patterns[:5],  # Limit to top 5 patterns
                "base_score": total_score
            }
        
        return scores
    
    def _adjust_weight_for_context(
        self, 
        data_type: DataType, 
        base_weight: float, 
        context_insights: Dict[str, Any]
    ) -> float:
        """
        Adjust pattern weight based on context insights
        
        Args:
            data_type: The data type being scored
            base_weight: Base pattern weight
            context_insights: Context insights for adjustment
            
        Returns:
            Adjusted weight value
        """
        adjusted_weight = base_weight
        
        # Boost weights for data types that align with conversation history
        previous_types = context_insights.get("previous_data_types", [])
        if data_type.value in previous_types:
            adjusted_weight *= 1.3  # 30% boost for recently seen types
        
        # Boost weights based on technical focus
        technical_focus = context_insights.get("technical_focus", [])
        if data_type == DataType.LOG_FILE and "containerization" in technical_focus:
            adjusted_weight *= 1.2
        elif data_type == DataType.CONFIG_FILE and any(
            focus in technical_focus for focus in ["api", "database"]
        ):
            adjusted_weight *= 1.15
        
        # Adjust based on user expertise
        user_expertise = context_insights.get("user_expertise", "unknown")
        if user_expertise == "expert":
            # Expert users tend to provide more structured data
            if data_type in [DataType.CONFIG_FILE, DataType.METRICS_DATA]:
                adjusted_weight *= 1.1
        elif user_expertise == "beginner":
            # Beginners more likely to provide error messages and logs
            if data_type in [DataType.ERROR_MESSAGE, DataType.LOG_FILE]:
                adjusted_weight *= 1.1
        
        return adjusted_weight
    
    def _apply_learned_patterns(
        self, 
        content: str, 
        context: Dict[str, Any]
    ) -> Dict[DataType, Dict[str, Any]]:
        """
        Apply learned patterns from previous classifications
        
        Args:
            content: Content to analyze
            context: Classification context
            
        Returns:
            Dictionary mapping DataType to learned pattern scores
        """
        scores = {}
        
        for data_type in DataType:
            if data_type in self._learned_patterns:
                total_score = 0.0
                matched_patterns = []
                
                for learned_pattern in self._learned_patterns[data_type]:
                    try:
                        pattern = re.compile(learned_pattern["pattern"], re.IGNORECASE)
                        matches = pattern.findall(content)
                        if matches:
                            confidence = learned_pattern.get("confidence", 0.5)
                            frequency = learned_pattern.get("frequency", 1)
                            score_contribution = len(matches) * confidence * frequency
                            total_score += score_contribution
                            matched_patterns.append(learned_pattern["pattern"])
                    except re.error:
                        # Skip invalid patterns
                        continue
                
                scores[data_type] = {
                    "score": total_score,
                    "patterns": matched_patterns[:3],  # Limit to top 3 learned patterns
                    "learned": True
                }
        
        return scores
    
    def _combine_classification_scores(
        self, 
        pattern_scores: Dict[DataType, Dict[str, Any]], 
        learned_scores: Dict[DataType, Dict[str, Any]], 
        context_insights: Dict[str, Any]
    ) -> Dict[DataType, Dict[str, Any]]:
        """
        Combine different scoring approaches for final classification
        
        Args:
            pattern_scores: Scores from pattern matching
            learned_scores: Scores from learned patterns
            context_insights: Context insights for weighting
            
        Returns:
            Combined scores for each data type
        """
        combined = {}
        
        all_types = set(pattern_scores.keys()) | set(learned_scores.keys())
        
        for data_type in all_types:
            pattern_score = pattern_scores.get(data_type, {"score": 0.0, "patterns": []})
            learned_score = learned_scores.get(data_type, {"score": 0.0, "patterns": []})
            
            # Weight learned patterns based on their reliability
            learned_weight = 0.3  # 30% weight for learned patterns
            pattern_weight = 0.7   # 70% weight for established patterns
            
            total_score = (
                pattern_score["score"] * pattern_weight + 
                learned_score["score"] * learned_weight
            )
            
            all_patterns = pattern_score["patterns"] + learned_score["patterns"]
            
            combined[data_type] = {
                "score": total_score,
                "patterns": all_patterns[:5],  # Limit to top 5 overall
                "pattern_score": pattern_score["score"],
                "learned_score": learned_score["score"]
            }
        
        return combined
    
    def _calculate_context_relevance(
        self, 
        classification: DataType, 
        context_insights: Dict[str, Any], 
        memory_context: Optional[ConversationContext]
    ) -> float:
        """
        Calculate how relevant the classification is to the conversation context
        
        Args:
            classification: The determined data type
            context_insights: Extracted context insights
            memory_context: Retrieved memory context
            
        Returns:
            Context relevance score (0.0 to 1.0)
        """
        if not memory_context:
            return 0.5  # Neutral relevance without context
        
        relevance = 0.5  # Base relevance
        
        # Check alignment with previous data types
        previous_types = context_insights.get("previous_data_types", [])
        if classification.value in previous_types:
            relevance += 0.2
        
        # Check alignment with technical focus
        technical_focus = context_insights.get("technical_focus", [])
        if technical_focus:
            if classification == DataType.LOG_FILE and "containerization" in technical_focus:
                relevance += 0.15
            elif classification == DataType.CONFIG_FILE and "api" in technical_focus:
                relevance += 0.15
            elif classification == DataType.ERROR_MESSAGE and any(
                focus in technical_focus for focus in ["database", "api"]
            ):
                relevance += 0.1
        
        # Check domain alignment
        domain_hints = context_insights.get("domain_hints", [])
        if domain_hints and classification != DataType.OTHER:
            relevance += 0.1
        
        return min(1.0, relevance)
    
    def _get_contributing_learned_patterns(
        self, 
        content: str, 
        classification: DataType
    ) -> List[str]:
        """
        Get learned patterns that contributed to the classification
        
        Args:
            content: Content that was classified
            classification: The determined classification
            
        Returns:
            List of contributing learned pattern descriptions
        """
        contributing = []
        
        if classification in self._learned_patterns:
            for learned_pattern in self._learned_patterns[classification]:
                try:
                    pattern = re.compile(learned_pattern["pattern"], re.IGNORECASE)
                    if pattern.search(content):
                        description = learned_pattern.get(
                            "description", 
                            f"Learned pattern: {learned_pattern['pattern'][:50]}"
                        )
                        contributing.append(description)
                except re.error:
                    continue
        
        return contributing[:3]  # Limit to top 3
    
    def _update_avg_metric(self, metric_name: str, new_value: float):
        """Update running average for a metric"""
        current_avg = self._metrics[metric_name]
        count = self._metrics["classifications"]
        
        if count == 1:
            self._metrics[metric_name] = new_value
        else:
            self._metrics[metric_name] = (current_avg * (count - 1) + new_value) / count
    
    async def learn_from_feedback(
        self, 
        content: str, 
        correct_classification: DataType, 
        predicted_classification: DataType,
        context: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Learn from user feedback to improve future classifications
        
        Args:
            content: The content that was classified
            correct_classification: The correct classification provided by user
            predicted_classification: The classification that was predicted
            context: Optional context information
            
        Returns:
            True if learning was successful
        """
        try:
            # Extract patterns from correctly classified content
            patterns_to_learn = self._extract_patterns_for_learning(
                content, correct_classification
            )
            
            # Update learned patterns
            for pattern_info in patterns_to_learn:
                self._add_learned_pattern(correct_classification, pattern_info)
            
            # Update pattern feedback for future improvements
            feedback_key = f"{predicted_classification.value}→{correct_classification.value}"
            if feedback_key not in self._pattern_feedback:
                self._pattern_feedback[feedback_key] = {"count": 0, "patterns": []}
            
            self._pattern_feedback[feedback_key]["count"] += 1
            self._pattern_feedback[feedback_key]["patterns"].extend(patterns_to_learn)
            
            self._metrics["learning_updates"] += 1
            
            self.logger.info(
                f"Learned from feedback: {predicted_classification.value} → {correct_classification.value}"
            )
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to learn from feedback: {e}")
            return False
    
    def _extract_patterns_for_learning(
        self, 
        content: str, 
        classification: DataType
    ) -> List[Dict[str, Any]]:
        """
        Extract learnable patterns from correctly classified content
        
        Args:
            content: The content to extract patterns from
            classification: The correct classification
            
        Returns:
            List of pattern information dictionaries
        """
        patterns = []
        
        # Extract unique tokens and sequences that might be indicative
        words = re.findall(r'\b\w+\b', content.lower())
        lines = content.split('\n')
        
        # Look for distinctive patterns in the content
        if classification == DataType.LOG_FILE:
            # Extract log-specific patterns
            for line in lines[:10]:  # Analyze first 10 lines
                if re.search(r'\d{4}-\d{2}-\d{2}', line):
                    # Extract timestamp format patterns
                    timestamp_match = re.search(r'\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}[^\s]*', line)
                    if timestamp_match:
                        patterns.append({
                            "pattern": re.escape(timestamp_match.group()[:19]),  # First 19 chars
                            "type": "timestamp",
                            "confidence": 0.8,
                            "frequency": 1,
                            "description": "Learned timestamp pattern"
                        })
        
        elif classification == DataType.ERROR_MESSAGE:
            # Extract error-specific patterns
            for line in lines:
                if any(keyword in line.lower() for keyword in ["error", "exception", "failed"]):
                    # Extract error signature patterns
                    error_words = [w for w in line.split() if len(w) > 3][:5]  # First 5 meaningful words
                    if error_words:
                        pattern = r'\b' + r'\b.*\b'.join(re.escape(w) for w in error_words) + r'\b'
                        patterns.append({
                            "pattern": pattern,
                            "type": "error_signature",
                            "confidence": 0.6,
                            "frequency": 1,
                            "description": f"Learned error pattern: {' '.join(error_words)}"
                        })
        
        elif classification == DataType.CONFIG_FILE:
            # Extract config-specific patterns
            for line in lines[:5]:  # First 5 lines
                if ':' in line and not line.strip().startswith('#'):
                    # YAML-like patterns
                    key_match = re.match(r'^(\s*\w+):', line)
                    if key_match:
                        patterns.append({
                            "pattern": re.escape(key_match.group(1)) + r':\s*',
                            "type": "config_key",
                            "confidence": 0.7,
                            "frequency": 1,
                            "description": f"Learned config key pattern"
                        })
        
        return patterns[:5]  # Limit to 5 patterns per learning session
    
    def _add_learned_pattern(self, data_type: DataType, pattern_info: Dict[str, Any]):
        """
        Add a learned pattern to the pattern database
        
        Args:
            data_type: The data type this pattern is associated with
            pattern_info: Pattern information dictionary
        """
        if data_type not in self._learned_patterns:
            self._learned_patterns[data_type] = []
        
        # Check if pattern already exists and update frequency
        existing_pattern = None
        for existing in self._learned_patterns[data_type]:
            if existing["pattern"] == pattern_info["pattern"]:
                existing_pattern = existing
                break
        
        if existing_pattern:
            # Update existing pattern
            existing_pattern["frequency"] += 1
            existing_pattern["confidence"] = min(
                1.0, 
                existing_pattern["confidence"] + 0.1
            )
        else:
            # Add new pattern
            self._learned_patterns[data_type].append(pattern_info.copy())
        
        # Limit number of learned patterns per type
        if len(self._learned_patterns[data_type]) > 20:
            # Remove patterns with lowest confidence
            self._learned_patterns[data_type].sort(
                key=lambda x: x["confidence"] * x["frequency"], 
                reverse=True
            )
            self._learned_patterns[data_type] = self._learned_patterns[data_type][:20]
    
    # Legacy interface compatibility
    @trace("enhanced_data_classifier_classify_legacy")
    async def classify(self, content: str, filename: Optional[str] = None) -> DataType:
        """
        Legacy classification method for backward compatibility
        
        Args:
            content: Raw content to classify
            filename: Optional filename for hints
            
        Returns:
            DataType enum value
        """
        # Use enhanced classification but return only the data type
        result = await self.classify_with_context(
            content=content,
            session_id="legacy",
            filename=filename,
            context=None
        )
        return result.data_type


class DataClassifier(IDataClassifier):
    """Classifies data content into appropriate DataType categories"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.llm_router = LLMRouter()

        # Heuristic patterns for classification
        self.patterns = {
            DataType.LOG_FILE: [
                # Timestamp patterns
                r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}",
                r"\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}",
                # Log level indicators
                r"\b(ERROR|WARN|WARNING|INFO|DEBUG|FATAL|CRITICAL)\b",
                # Common log prefixes
                r"^\d{4}-\d{2}-\d{2}",
                r"^\[\d{4}-\d{2}-\d{2}",
                # Log file extensions
                r"\.(log|txt)$",
            ],
            DataType.ERROR_REPORT: [
                # Error keywords
                r"\b(error|exception|failed|failure|crash|abort)\b",
                # Exception patterns
                r"Exception:",
                r"Error:",
                r"Traceback \(most recent call last\):",
                # HTTP error codes
                r"\b(4\d{2}|5\d{2})\b",
                # Stack trace patterns
                r"at\s+[\w\.$<>]+\([^)]*\)",
                r'File "[^"]+", line \d+',
                r'^\s+File\s+"[^"]+"',
                r"^\s+at\s+",
            ],
            DataType.CONFIG_FILE: [
                # YAML indicators
                r"^---\s*$",
                r"^\w+:\s*$",
                r"^\s+-\s+\w+:",
                # JSON config
                r'\{[^{}]*"config":',
                r'\{[^{}]*"settings":',
                # INI format
                r"^\[[^\]]+\]",
                r"^\w+\s*=\s*[^=]+$",
                # Environment variables
                r"^\w+=\w+$",
            ],
            DataType.DOCUMENTATION: [
                # Markdown
                r"^#\s+\w+",
                r"^\*\*[^*]+\*\*",
                r"^\[[^\]]+\]\([^)]+\)",
                # Documentation keywords
                r"\b(guide|manual|documentation|tutorial|help)\b",
                # HTML tags
                r"<[^>]+>",
                # Code blocks
                r"```\w*",
            ],
            DataType.SCREENSHOT: [
                # Image file indicators
                r"\.(png|jpg|jpeg|gif|bmp|webp)$",
                # Base64 image data patterns
                r"data:image/[^;]+;base64,",
                # Image metadata patterns
                r"\b(width|height|resolution|pixels)\b",
            ],
            DataType.OTHER: [
                # Catch-all patterns
                r"^.{1,100}$",  # Short content
                r"^\s*$",       # Empty content
            ],
        }

        # Compile patterns for efficiency
        self.compiled_patterns = {}
        for data_type, pattern_list in self.patterns.items():
            self.compiled_patterns[data_type] = [
                re.compile(pattern, re.IGNORECASE | re.MULTILINE)
                for pattern in pattern_list
            ]

    @trace("data_classifier_classify")
    async def classify(self, content: str, filename: Optional[str] = None) -> DataType:
        """
        Classify data content into appropriate DataType

        Args:
            content: Raw content to classify
            filename: Optional filename for additional classification hints

        Returns:
            DataType enum value
        """
        if not content or not isinstance(content, str):
            return DataType.OTHER

        # Try heuristic classification first (including filename hints)
        heuristic_result = self._heuristic_classify(content, filename)
        if heuristic_result != DataType.OTHER:
            self.logger.info(f"Heuristic classification: {heuristic_result}")
            return heuristic_result

        # Fallback to LLM-based classification
        try:
            llm_result = await self._llm_classify(content)
            self.logger.info(f"LLM classification: {llm_result}")
            return llm_result
        except Exception as e:
            self.logger.warning(f"LLM classification failed: {e}")
            return DataType.OTHER

    def _heuristic_classify(self, content: str, filename: Optional[str] = None) -> DataType:
        """
        Perform heuristic-based classification using regex patterns and filename hints

        Args:
            content: Content to classify
            filename: Optional filename for additional hints

        Returns:
            DataType enum value
        """
        # Early classification based on filename extension if available
        if filename:
            filename_lower = filename.lower()
            if filename_lower.endswith(('.log', '.txt')):
                # Still check content patterns, but boost log file confidence
                pass  # Continue with pattern analysis below
            elif filename_lower.endswith(('.json', '.yaml', '.yml', '.ini', '.conf', '.config')):
                return DataType.CONFIG_FILE
            elif filename_lower.endswith(('.md', '.rst', '.txt', '.html', '.htm')):
                # Could be documentation, but verify with content patterns
                if any(keyword in content.lower() for keyword in ['guide', 'manual', 'documentation', 'tutorial', 'help', 'readme']):
                    return DataType.DOCUMENTATION
            elif filename_lower.endswith(('.csv', '.tsv')):
                # Check if it's metrics data
                if self._is_csv_with_metrics(content):
                    return DataType.METRICS_DATA
                return DataType.CONFIG_FILE
            elif any(ext in filename_lower for ext in ['error', 'exception', 'crash']):
                return DataType.ERROR_REPORT

        # Calculate confidence scores for each data type
        scores = {}

        for data_type, patterns in self.compiled_patterns.items():
            score = 0
            for pattern in patterns:
                matches = pattern.findall(content)
                score += len(matches)

            # Normalize score by content length
            if len(content) > 0:
                score = score / len(content) * 1000  # Scale up for readability

            scores[data_type] = score

        # Apply priority rules for overlapping types
        log_score = scores.get(DataType.LOG_FILE, 0)
        error_score = scores.get(DataType.ERROR_REPORT, 0)

        # If we have both log patterns and error reports, prioritize LOG_FILE
        # when timestamps and log levels are present
        if (
            log_score > 0.1
            and error_score > 0
            and self._has_timestamps(content)
            and self._has_log_levels(content)
        ):
            return DataType.LOG_FILE

        # Find the data type with highest score
        if scores:
            best_type = max(scores.items(), key=lambda x: x[1])
            if best_type[1] > 0.1:  # Threshold for confidence
                return best_type[0]

        # Additional heuristics for specific formats
        if self._is_json(content):
            return DataType.CONFIG_FILE
        elif self._is_yaml(content):
            return DataType.CONFIG_FILE
        elif self._is_csv_with_metrics(content):
            # For now, classify CSV metrics as OTHER since we don't have METRICS_DATA in our simplified enum
            return DataType.OTHER

        return DataType.OTHER

    def _has_timestamps(self, content: str) -> bool:
        """Check if content has timestamp patterns"""
        timestamp_patterns = [
            r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}",
            r"\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}",
        ]
        for pattern in timestamp_patterns:
            if re.search(pattern, content):
                return True
        return False

    def _has_log_levels(self, content: str) -> bool:
        """Check if content has log level indicators"""
        return bool(
            re.search(
                r"\b(ERROR|WARN|WARNING|INFO|DEBUG|FATAL|CRITICAL)\b",
                content,
                re.IGNORECASE,
            )
        )

    def _has_stack_trace_format(self, content: str) -> bool:
        """Check if content has actual stack trace formatting"""
        stack_patterns = [
            r"Traceback \(most recent call last\):",
            r"^\s+at\s+[\w\.$<>]+\([^)]*\)",
            r'^\s+File\s+"[^"]+"',
        ]
        for pattern in stack_patterns:
            if re.search(pattern, content, re.MULTILINE):
                return True
        return False

    def _is_json(self, content: str) -> bool:
        """Check if content is valid JSON"""
        try:
            json.loads(content)
            return True
        except (json.JSONDecodeError, ValueError):
            return False

    def _is_yaml(self, content: str) -> bool:
        """Check if content is valid YAML"""
        try:
            yaml.safe_load(content)
            return True
        except (yaml.YAMLError, ValueError):
            return False

    def _is_csv_with_metrics(self, content: str) -> bool:
        """Check if content is CSV with numeric metrics"""
        lines = content.strip().split("\n")
        if len(lines) < 2:
            return False

        # Check if first few lines contain numeric data
        numeric_lines = 0
        for line in lines[:5]:
            if re.match(r"^[^,]*,\d+\.?\d*", line):
                numeric_lines += 1

        return numeric_lines >= 2

    async def _llm_classify(self, content: str) -> DataType:
        """
        Use LLM to classify content when heuristics fail

        Args:
            content: Content to classify

        Returns:
            DataType enum value
        """
        # Create classification prompt
        prompt = f"""
        Classify the following data content into one of these categories:
        - log_file: System or application logs with timestamps
        - error_report: Error messages, exception details, or crash reports
        - config_file: Configuration files (YAML, JSON, INI, etc.)
        - documentation: Documentation, guides, manuals, help text
        - screenshot: Image files or visual content
        - other: Any other type of content that doesn't fit above categories
        
        Content to classify:
        {content[:1000]}  # Limit content length
        
        Respond with only the category name (e.g., "log_file").
        """

        try:
            response = await self.llm_router.route(
                prompt=prompt,
                max_tokens=50,
                temperature=0.1,  # Low temperature for classification
            )

            # Parse response
            category = response.content.strip().lower()

            # Map response to DataType
            category_mapping = {
                "log_file": DataType.LOG_FILE,
                "error_report": DataType.ERROR_REPORT,
                "config_file": DataType.CONFIG_FILE,
                "documentation": DataType.DOCUMENTATION,
                "screenshot": DataType.SCREENSHOT,
                "other": DataType.OTHER,
            }

            return category_mapping.get(category, DataType.OTHER)

        except Exception as e:
            self.logger.error(f"LLM classification failed: {e}")
            return DataType.OTHER

    def get_classification_confidence(self, content: str, data_type: DataType) -> float:
        """
        Get confidence score for a specific classification

        Args:
            content: Content that was classified
            data_type: The DataType to check confidence for

        Returns:
            Confidence score between 0.0 and 1.0
        """
        if data_type not in self.compiled_patterns:
            return 0.0

        patterns = self.compiled_patterns[data_type]
        total_matches = 0

        for pattern in patterns:
            matches = pattern.findall(content)
            total_matches += len(matches)

        # Normalize by content length and pattern count
        if len(content) > 0 and patterns:
            confidence = min(1.0, total_matches / (len(content) / 1000))
            return confidence

        return 0.0
