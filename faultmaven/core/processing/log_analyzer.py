"""Enhanced Log Analyzer - Phase 3

Purpose: Memory-aware log analysis with context understanding and pattern learning

Requirements:
--------------------------------------------------------------------------------
• Memory-enhanced log processing using conversation context
• Pattern learning from user feedback and historical analysis
• Context-aware anomaly detection and insight extraction
• Enhanced security assessment for PII detection in logs
• Integration with memory service for historical patterns and user preferences

Key Components:
--------------------------------------------------------------------------------
  class EnhancedLogProcessor: Memory-aware log processor with pattern learning
  def process_with_context(content: str, context: dict) -> EnhancedProcessingResult
  def learn_from_feedback(processing_result: dict, feedback: dict)
  def _memory_aware_anomaly_detection(df: DataFrame, memory_context: dict)

Technology Stack:
--------------------------------------------------------------------------------
pandas, PyOD, scikit-learn, MemoryService, PatternLearner

Core Design Principles:
--------------------------------------------------------------------------------
• Memory-Aware: Use conversation context for better log analysis
• Learning: Continuously improve from user interactions and feedback
• Privacy-First: Enhanced PII detection and sanitization in logs
• Resilience: Implement retries and fallbacks with memory
• Cost-Efficiency: Use semantic caching with context awareness
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from dataclasses import dataclass
from collections import defaultdict, deque

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from faultmaven.models import AgentState, DataInsightsResponse, DataType
from faultmaven.models.interfaces import ILogProcessor, IMemoryService, ConversationContext
from faultmaven.infrastructure.observability.tracing import trace


@dataclass
class EnhancedProcessingResult:
    """Enhanced processing result with context and learning information"""
    insights: Dict[str, Any]
    anomalies: List[Dict[str, Any]]
    recommendations: List[str]
    confidence_score: float
    context_relevance: float
    memory_enhanced: bool
    security_flags: List[str]
    learned_patterns: List[str]
    processing_time_ms: float
    pattern_matches: List[str]


class EnhancedLogProcessor(ILogProcessor):
    """Memory-aware log processor with pattern learning and context understanding"""

    def __init__(self, memory_service: Optional[IMemoryService] = None):
        self.logger = logging.getLogger(__name__)
        self._memory_service = memory_service
        
        # Pattern learning components
        self._learned_anomaly_patterns = defaultdict(list)
        self._learned_error_patterns = defaultdict(list)
        self._processing_history = deque(maxlen=1000)
        self._user_preferences = defaultdict(dict)
        
        # Performance metrics
        self._metrics = {
            "processes": 0,
            "memory_enhanced": 0,
            "anomalies_detected": 0,
            "patterns_learned": 0,
            "avg_confidence": 0.0,
            "avg_context_relevance": 0.0,
            "avg_processing_time": 0.0
        }

        # Enhanced log patterns with weights for memory-aware processing
        self.enhanced_log_patterns = {
            "timestamp": [
                (r"(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)", 3.0),
                (r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})", 2.5),
                (r"(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2})", 2.0),
            ],
            "log_level": [
                (r"\b(FATAL|CRITICAL)\b", 4.0),  # Higher weight for severe levels
                (r"\b(ERROR)\b", 3.5),
                (r"\b(WARN|WARNING)\b", 2.5),
                (r"\b(INFO)\b", 2.0),
                (r"\b(DEBUG|TRACE)\b", 1.5),
            ],
            "http_status": [
                (r"\b(5\d{2})\b", 3.5),  # Server errors get higher weight
                (r"\b(4\d{2})\b", 3.0),  # Client errors
                (r"\b(3\d{2})\b", 2.0),  # Redirects
                (r"\b(2\d{2})\b", 1.5),  # Success codes
            ],
            "ip_address": [
                (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", 2.0),
            ],
            "error_code": [
                (r"\b[A-Z_]+_ERROR\b", 3.0),
                (r"\b[A-Z_]+_EXCEPTION\b", 3.0),
                (r"\b[A-Z_]+_FAILURE\b", 2.5),
            ],
            "duration": [
                (r"(\d+(?:\.\d+)?)\s*(?:ms|milliseconds?)", 2.5),
                (r"(\d+(?:\.\d+)?)\s*(?:s|seconds?)", 2.0),
            ],
            "kubernetes": [
                (r"pod/[\w-]+", 2.5),
                (r"namespace/[\w-]+", 2.5),
                (r"deployment/[\w-]+", 2.0),
                (r"service/[\w-]+", 2.0),
            ],
            "database": [
                (r"connection\s+(?:timeout|refused|failed)", 3.0),
                (r"deadlock", 3.5),
                (r"transaction\s+(?:rollback|timeout)", 3.0),
                (r"table\s+[\w.]+", 2.0),
            ]
        }

        # Compile patterns with weights
        self.compiled_enhanced_patterns = {}
        for category, patterns in self.enhanced_log_patterns.items():
            self.compiled_enhanced_patterns[category] = [
                (re.compile(pattern, re.IGNORECASE), weight)
                for pattern, weight in patterns
            ]

        # Security patterns for PII detection in logs
        self.security_patterns = {
            "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
            "phone": re.compile(r"\b\d{3}-\d{3}-\d{4}\b|\b\(\d{3}\)\s*\d{3}-\d{4}\b"),
            "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
            "credit_card": re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),
            "api_key": re.compile(r"(?:api[_-]?key|token)[:\s=]+['\"]?[A-Za-z0-9]{16,}['\"]?", re.IGNORECASE),
            "password": re.compile(r"(?:password|passwd|pwd)[:\s=]+['\"]?[^\s'\"]+['\"]?", re.IGNORECASE),
            "session_id": re.compile(r"(?:session[_-]?id|sessionid)[:\s=]+['\"]?[A-Za-z0-9]{16,}['\"]?", re.IGNORECASE),
            "bearer_token": re.compile(r"bearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
            "jwt": re.compile(r"ey[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
            "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
        }

        # Standard log patterns (for backward compatibility)
        self.log_patterns = {
            "timestamp": [pattern for pattern, _ in self.enhanced_log_patterns["timestamp"]],
            "log_level": [pattern for pattern, _ in self.enhanced_log_patterns["log_level"]],
            "http_status": [pattern for pattern, _ in self.enhanced_log_patterns["http_status"]],
            "ip_address": [pattern for pattern, _ in self.enhanced_log_patterns["ip_address"]],
            "error_code": [pattern for pattern, _ in self.enhanced_log_patterns["error_code"]],
            "duration": [pattern for pattern, _ in self.enhanced_log_patterns["duration"]],
        }

        # Compile standard patterns
        self.compiled_patterns = {}
        for category, patterns in self.log_patterns.items():
            self.compiled_patterns[category] = [
                re.compile(pattern, re.IGNORECASE) for pattern in patterns
            ]

    @trace("enhanced_log_processor_process_with_context")
    async def process_with_context(
        self,
        content: str,
        session_id: str,
        data_type: Optional[DataType] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> EnhancedProcessingResult:
        """
        Memory-aware log processing with conversation context integration
        
        This method leverages conversation history, user preferences, and domain
        context from the memory system to provide more accurate and relevant
        log analysis with personalized insights and recommendations.
        
        Args:
            content: Raw log content to process
            session_id: Session identifier for memory context retrieval
            data_type: Optional data type hint for processing optimization
            context: Optional additional context data
            
        Returns:
            EnhancedProcessingResult with memory-enhanced analysis
        """
        start_time = time.time()
        
        # Retrieve memory context if available
        memory_context = None
        memory_enhanced = False
        
        if self._memory_service and session_id:
            try:
                # Create a query from log content preview for context retrieval
                content_preview = content[:300] + "..." if len(content) > 300 else content
                memory_context = await self._memory_service.retrieve_context(session_id, content_preview)
                memory_enhanced = True
                self._metrics["memory_enhanced"] += 1
            except Exception as e:
                self.logger.warning(f"Failed to retrieve memory context for log processing: {e}")
                memory_context = None
        
        # Combine context sources
        combined_context = {
            "memory": memory_context,
            "additional": context or {},
            "session_id": session_id,
            "data_type": data_type
        }
        
        # Perform enhanced log processing
        result = await self._process_with_memory_context(content, combined_context, start_time)
        
        # Update metrics
        self._metrics["processes"] += 1
        self._update_avg_metric("avg_confidence", result.confidence_score)
        self._update_avg_metric("avg_context_relevance", result.context_relevance)
        self._update_avg_metric("avg_processing_time", result.processing_time_ms)
        
        # Store processing for learning
        self._processing_history.append({
            "content_hash": hash(content) % 1000000,
            "result": result,
            "context": combined_context,
            "timestamp": time.time()
        })
        
        return result
    
    async def _process_with_memory_context(
        self,
        content: str,
        context: Dict[str, Any],
        start_time: float
    ) -> EnhancedProcessingResult:
        """
        Perform log processing enhanced with memory context
        
        Args:
            content: Log content to process
            context: Combined context including memory and additional data
            start_time: Start time for performance measurement
            
        Returns:
            EnhancedProcessingResult with memory-enhanced processing
        """
        try:
            # Parse logs into structured format with enhanced patterns
            df = self._parse_logs_with_context(content, context)
            
            if df.empty:
                return EnhancedProcessingResult(
                    insights={"error": "No valid log entries found", "total_entries": 0},
                    anomalies=[],
                    recommendations=["Check log format and content validity"],
                    confidence_score=0.0,
                    context_relevance=0.0,
                    memory_enhanced=False,
                    security_flags=[],
                    learned_patterns=[],
                    processing_time_ms=(time.time() - start_time) * 1000,
                    pattern_matches=[]
                )
            
            # Detect security issues first
            security_flags = self._detect_log_security_issues(content)
            
            # Extract memory context insights for processing
            memory_context = context.get("memory")
            context_insights = self._extract_log_context_insights(memory_context, content)
            
            # Enhanced insights extraction with context awareness
            insights = self._extract_memory_aware_insights(df, context_insights)
            
            # Enhanced anomaly detection with memory context
            anomalies = self._detect_context_aware_anomalies(df, context_insights)
            
            # Apply learned patterns for anomaly detection
            learned_anomalies = self._apply_learned_anomaly_patterns(df, context)
            anomalies.extend(learned_anomalies)
            
            # Generate context-aware recommendations
            recommendations = self._generate_memory_aware_recommendations(
                insights, anomalies, context_insights, memory_context
            )
            
            # Calculate confidence and context relevance
            confidence_score = self._calculate_processing_confidence(df, insights, anomalies, context_insights)
            context_relevance = self._calculate_log_context_relevance(insights, context_insights, memory_context)
            
            # Get contributing learned patterns
            learned_patterns = self._get_contributing_log_patterns(content, insights)
            
            # Get pattern matches for transparency
            pattern_matches = self._get_pattern_matches(content, context_insights)
            
            processing_time = (time.time() - start_time) * 1000
            
            # Update anomaly detection metrics
            self._metrics["anomalies_detected"] += len(anomalies)
            
            return EnhancedProcessingResult(
                insights=insights,
                anomalies=anomalies,
                recommendations=recommendations,
                confidence_score=confidence_score,
                context_relevance=context_relevance,
                memory_enhanced=context.get("memory") is not None,
                security_flags=security_flags,
                learned_patterns=learned_patterns,
                processing_time_ms=processing_time,
                pattern_matches=pattern_matches
            )
            
        except Exception as e:
            self.logger.error(f"Enhanced log processing failed: {e}")
            return EnhancedProcessingResult(
                insights={"error": str(e), "processing_error": True},
                anomalies=[],
                recommendations=["Review log content and processing parameters"],
                confidence_score=0.0,
                context_relevance=0.0,
                memory_enhanced=False,
                security_flags=[],
                learned_patterns=[],
                processing_time_ms=(time.time() - start_time) * 1000,
                pattern_matches=[]
            )
    
    def _detect_log_security_issues(self, content: str) -> List[str]:
        """
        Detect potential security and PII issues in log content
        
        Args:
            content: Log content to analyze for security issues
            
        Returns:
            List of detected security flags
        """
        flags = []
        
        for security_type, pattern in self.security_patterns.items():
            matches = pattern.findall(content)
            if matches:
                flags.append(f"{security_type}_detected")
                # Log first few matches for debugging (sanitized)
                sample_count = min(3, len(matches))
                self.logger.warning(
                    f"Detected {security_type} in logs: {sample_count} instances found"
                )
        
        return flags
    
    def _extract_log_context_insights(
        self,
        memory_context: Optional[ConversationContext],
        content: str
    ) -> Dict[str, Any]:
        """
        Extract log processing insights from memory context
        
        Args:
            memory_context: Retrieved conversation context
            content: Log content being processed
            
        Returns:
            Dictionary of context insights for log processing
        """
        insights = {
            "technical_focus": [],
            "user_expertise": "unknown",
            "previous_issues": [],
            "service_context": [],
            "urgency_level": "normal",
            "expected_patterns": [],
            "investigation_keywords": []
        }
        
        if not memory_context:
            return insights
        
        # Extract technical focus from conversation history
        if memory_context.conversation_history:
            for item in memory_context.conversation_history[-10:]:  # Last 10 items
                content_lower = item.get("content", "").lower()
                
                # Identify technical domains
                if any(term in content_lower for term in ["kubernetes", "k8s", "docker", "container"]):
                    insights["technical_focus"].append("containerization")
                if any(term in content_lower for term in ["database", "sql", "mysql", "postgres", "db"]):
                    insights["technical_focus"].append("database")
                if any(term in content_lower for term in ["api", "rest", "graphql", "endpoint", "service"]):
                    insights["technical_focus"].append("api")
                if any(term in content_lower for term in ["network", "connection", "timeout", "latency"]):
                    insights["technical_focus"].append("network")
                if any(term in content_lower for term in ["memory", "cpu", "disk", "performance"]):
                    insights["technical_focus"].append("performance")
                
                # Extract previous issue patterns
                if any(term in content_lower for term in ["error", "failure", "crash", "down"]):
                    insights["previous_issues"].append("errors")
                if any(term in content_lower for term in ["slow", "timeout", "latency", "performance"]):
                    insights["previous_issues"].append("performance")
                if any(term in content_lower for term in ["security", "breach", "unauthorized", "attack"]):
                    insights["previous_issues"].append("security")
                
                # Extract service context
                services = re.findall(r'\b(\w+[-_]?\w*service|\w+[-_]?\w*api|\w+[-_]?\w*server)\b', content_lower)
                insights["service_context"].extend(services[:5])  # Limit to 5 services
                
                # Determine urgency level
                if any(term in content_lower for term in ["urgent", "critical", "emergency", "down", "outage"]):
                    insights["urgency_level"] = "high"
                elif any(term in content_lower for term in ["important", "priority", "investigate"]):
                    insights["urgency_level"] = "medium"
                
                # Extract investigation keywords
                keywords = re.findall(r'\b(\w{4,})\b', content_lower)
                insights["investigation_keywords"].extend(keywords[:10])  # Limit to 10 keywords
        
        # Extract user expertise level
        if memory_context.user_profile:
            insights["user_expertise"] = memory_context.user_profile.get("skill_level", "unknown")
        
        # Clean up duplicates
        for key in ["technical_focus", "previous_issues", "service_context", "investigation_keywords"]:
            insights[key] = list(set(insights[key]))
        
        return insights
    
    def _parse_logs_with_context(
        self,
        content: str,
        context: Dict[str, Any]
    ) -> pd.DataFrame:
        """
        Parse log content with context-aware pattern recognition
        
        Args:
            content: Raw log content
            context: Processing context including memory insights
            
        Returns:
            DataFrame with parsed log entries enhanced with context
        """
        lines = content.strip().split("\n")
        parsed_entries = []
        
        # Get context insights for enhanced parsing
        memory_context = context.get("memory")
        if memory_context:
            context_insights = self._extract_log_context_insights(memory_context, content)
        else:
            context_insights = {}
        
        for line_num, line in enumerate(lines, 1):
            if not line.strip():
                continue
            
            entry = self._parse_log_line_with_context(line, line_num, context_insights)
            if entry:
                parsed_entries.append(entry)
        
        return pd.DataFrame(parsed_entries)
    
    def _parse_log_line_with_context(
        self,
        line: str,
        line_num: int,
        context_insights: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Parse a single log line with context awareness
        
        Args:
            line: Single log line
            line_num: Line number for reference
            context_insights: Context insights for enhanced parsing
            
        Returns:
            Parsed log entry dictionary with context enhancements
        """
        if line is None:
            return None
        
        entry = {
            "line_number": line_num,
            "raw_line": line,
            "timestamp": None,
            "log_level": None,
            "message": line,
            "http_status": None,
            "ip_address": None,
            "error_code": None,
            "duration_ms": None,
            "service": None,
            "component": None,
            "context_relevance": 0.0,
            "severity_weight": 1.0
        }
        
        # Enhanced pattern matching with context weights
        technical_focus = context_insights.get("technical_focus", [])
        urgency_level = context_insights.get("urgency_level", "normal")
        investigation_keywords = context_insights.get("investigation_keywords", [])
        
        # Standard field extraction with enhanced patterns
        for category, patterns in self.compiled_enhanced_patterns.items():
            for pattern, weight in patterns:
                match = pattern.search(line)
                if match:
                    if category == "timestamp":
                        entry["timestamp"] = match.group(1)
                    elif category == "log_level":
                        entry["log_level"] = match.group(1).upper()
                        # Adjust severity weight based on log level and context
                        if entry["log_level"] in ["FATAL", "CRITICAL"]:
                            entry["severity_weight"] = 4.0
                        elif entry["log_level"] == "ERROR":
                            entry["severity_weight"] = 3.0
                        elif entry["log_level"] in ["WARN", "WARNING"]:
                            entry["severity_weight"] = 2.0
                    elif category == "http_status":
                        entry["http_status"] = int(match.group(1))
                    elif category == "ip_address":
                        entry["ip_address"] = match.group(0)
                    elif category == "error_code":
                        entry["error_code"] = match.group(0)
                    elif category == "duration":
                        try:
                            duration_str = match.group(1)
                            duration = float(duration_str)
                            if "ms" in line.lower():
                                entry["duration_ms"] = duration
                            else:
                                entry["duration_ms"] = duration * 1000  # Convert to ms
                        except ValueError:
                            pass
                    elif category == "kubernetes" and "containerization" in technical_focus:
                        # Extract Kubernetes-specific information
                        k8s_resource = match.group(0)
                        if k8s_resource.startswith("pod/"):
                            entry["component"] = k8s_resource
                        elif k8s_resource.startswith("service/"):
                            entry["service"] = k8s_resource
                    elif category == "database" and "database" in technical_focus:
                        # Database-specific parsing
                        entry["component"] = "database"
                        if "deadlock" in match.group(0).lower():
                            entry["severity_weight"] = 3.5
        
        # Context relevance scoring
        entry["context_relevance"] = self._calculate_line_context_relevance(
            line, context_insights
        )
        
        # Adjust severity weight based on context
        if urgency_level == "high":
            entry["severity_weight"] *= 1.3
        elif urgency_level == "medium":
            entry["severity_weight"] *= 1.1
        
        # Check for investigation keywords
        if investigation_keywords:
            line_lower = line.lower()
            keyword_matches = sum(1 for keyword in investigation_keywords if keyword in line_lower)
            if keyword_matches > 0:
                entry["context_relevance"] += min(0.3, keyword_matches * 0.1)
        
        return entry
    
    def _calculate_line_context_relevance(
        self,
        line: str,
        context_insights: Dict[str, Any]
    ) -> float:
        """
        Calculate context relevance for a single log line
        
        Args:
            line: Log line content
            context_insights: Context insights from memory
            
        Returns:
            Context relevance score (0.0 to 1.0)
        """
        relevance = 0.0
        line_lower = line.lower()
        
        # Check technical focus alignment
        technical_focus = context_insights.get("technical_focus", [])
        for focus in technical_focus:
            if focus == "containerization" and any(
                term in line_lower for term in ["pod", "container", "k8s", "kubernetes", "docker"]
            ):
                relevance += 0.2
            elif focus == "database" and any(
                term in line_lower for term in ["db", "database", "sql", "query", "connection"]
            ):
                relevance += 0.2
            elif focus == "api" and any(
                term in line_lower for term in ["api", "endpoint", "rest", "http", "request"]
            ):
                relevance += 0.2
            elif focus == "network" and any(
                term in line_lower for term in ["network", "connection", "timeout", "latency"]
            ):
                relevance += 0.2
            elif focus == "performance" and any(
                term in line_lower for term in ["memory", "cpu", "performance", "slow", "timeout"]
            ):
                relevance += 0.2
        
        # Check previous issues alignment
        previous_issues = context_insights.get("previous_issues", [])
        for issue in previous_issues:
            if issue == "errors" and any(
                term in line_lower for term in ["error", "exception", "failed", "failure"]
            ):
                relevance += 0.15
            elif issue == "performance" and any(
                term in line_lower for term in ["slow", "timeout", "latency", "performance"]
            ):
                relevance += 0.15
            elif issue == "security" and any(
                term in line_lower for term in ["security", "unauthorized", "forbidden", "breach"]
            ):
                relevance += 0.15
        
        # Check service context
        service_context = context_insights.get("service_context", [])
        for service in service_context:
            if service.lower() in line_lower:
                relevance += 0.1
        
        return min(1.0, relevance)
    
    def _update_avg_metric(self, metric_name: str, new_value: float):
        """Update running average for a metric"""
        current_avg = self._metrics[metric_name]
        count = self._metrics["processes"]
        
        if count == 1:
            self._metrics[metric_name] = new_value
        else:
            self._metrics[metric_name] = (current_avg * (count - 1) + new_value) / count
    
    def _extract_memory_aware_insights(self, df: pd.DataFrame, context_insights: Dict[str, Any]) -> Dict[str, Any]:
        """Extract insights enhanced with memory context"""
        # Use the standard extraction but enhance with context
        insights = self._extract_basic_insights(df, context_insights)
        
        # Add memory-aware enhancements
        if "context_relevance" in df.columns:
            insights["context_analysis"] = {
                "avg_relevance": df["context_relevance"].mean(),
                "high_relevance_entries": len(df[df["context_relevance"] > 0.7]),
                "relevant_percentage": len(df[df["context_relevance"] > 0.3]) / len(df) * 100
            }
        
        return insights
    
    def _detect_context_aware_anomalies(self, df: pd.DataFrame, context_insights: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Detect anomalies with context awareness"""
        # Use the standard detection but enhance with context
        anomalies = self._detect_anomalies(df)
        
        # Add context-aware anomaly detection
        if "severity_weight" in df.columns:
            high_severity = df[df["severity_weight"] > 3.0]
            if len(high_severity) > 0:
                anomalies.append({
                    "type": "high_severity_events",
                    "severity": "high",
                    "description": f"Detected {len(high_severity)} high-severity log entries",
                    "value": len(high_severity),
                    "context_aware": True
                })
        
        return anomalies
    
    def _apply_learned_anomaly_patterns(self, df: pd.DataFrame, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Apply learned patterns for anomaly detection"""
        anomalies = []
        
        # This would be enhanced with actual learned patterns
        # For now, return empty list
        return anomalies
    
    def _generate_memory_aware_recommendations(
        self, 
        insights: Dict[str, Any], 
        anomalies: List[Dict[str, Any]], 
        context_insights: Dict[str, Any], 
        memory_context: Optional[ConversationContext]
    ) -> List[str]:
        """Generate recommendations enhanced with memory context"""
        # Use the standard generation but enhance with context
        recommendations = self._generate_recommendations(insights, anomalies, context_insights)
        
        # Add memory-aware recommendations
        user_expertise = context_insights.get("user_expertise", "unknown")
        if user_expertise == "beginner":
            recommendations.append("Consider reviewing log analysis basics for better understanding")
        elif user_expertise == "expert":
            recommendations.append("Focus on advanced pattern analysis and correlation with other systems")
        
        return recommendations
    
    def _calculate_processing_confidence(
        self, 
        df: pd.DataFrame, 
        insights: Dict[str, Any], 
        anomalies: List[Dict[str, Any]], 
        context_insights: Dict[str, Any]
    ) -> float:
        """Calculate confidence score for processing"""
        # Use the standard calculation but enhance with context
        confidence = self._calculate_confidence(df, insights, anomalies)
        
        # Adjust based on context relevance
        if "context_analysis" in insights:
            avg_relevance = insights["context_analysis"].get("avg_relevance", 0.0)
            confidence = (confidence + avg_relevance) / 2
        
        return confidence
    
    def _calculate_log_context_relevance(
        self, 
        insights: Dict[str, Any], 
        context_insights: Dict[str, Any], 
        memory_context: Optional[ConversationContext]
    ) -> float:
        """Calculate context relevance for log processing"""
        if not memory_context:
            return 0.5  # Neutral relevance without context
        
        relevance = 0.5
        
        # Boost relevance based on context alignment
        if context_insights.get("technical_focus"):
            relevance += 0.2
        
        if context_insights.get("previous_issues"):
            relevance += 0.15
        
        if context_insights.get("service_context"):
            relevance += 0.1
        
        # Context analysis from insights
        if "context_analysis" in insights:
            avg_relevance = insights["context_analysis"].get("avg_relevance", 0.0)
            relevance = (relevance + avg_relevance) / 2
        
        return min(1.0, relevance)
    
    def _get_contributing_log_patterns(self, content: str, insights: Dict[str, Any]) -> List[str]:
        """Get patterns that contributed to the processing"""
        patterns = []
        
        # This would be enhanced with actual learned patterns
        # For now, return basic patterns based on insights
        if "error_summary" in insights and insights["error_summary"].get("total_errors", 0) > 0:
            patterns.append("Error pattern detection")
        
        if "performance_metrics" in insights:
            patterns.append("Performance pattern analysis")
        
        return patterns
    
    def _get_pattern_matches(self, content: str, context_insights: Dict[str, Any]) -> List[str]:
        """Get pattern matches for transparency"""
        matches = []
        
        # Check which enhanced patterns matched
        for category, patterns in self.compiled_enhanced_patterns.items():
            for pattern, weight in patterns:
                if pattern.search(content):
                    matches.append(f"{category}_pattern")
        
        return list(set(matches))  # Remove duplicates
    
    # Standard methods used by enhanced processing
    def _extract_basic_insights(self, df: pd.DataFrame, agent_state: Dict[str, Any]) -> Dict[str, Any]:
        """Extract basic insights from parsed log data with context awareness"""
        insights: Dict[str, Any] = {
            "total_entries": len(df),
            "time_range": None,
            "log_level_distribution": {},
            "error_summary": {},
            "performance_metrics": {},
            "top_errors": [],
            "unique_ips": 0,
            "contextual_analysis": {},
        }

        # Extract context keywords from agent state
        context_keywords = []
        investigation_context = agent_state.get("investigation_context", {})

        # Get keywords from various context sources
        if "keywords" in investigation_context:
            context_keywords.extend(investigation_context["keywords"])
        if "services" in investigation_context:
            context_keywords.extend(investigation_context["services"])
        if "components" in investigation_context:
            context_keywords.extend(investigation_context["components"])

        # Extract keywords from user query
        user_query = agent_state.get("user_query", "")
        if user_query:
            # Simple keyword extraction from user query
            query_words = [
                word.lower()
                for word in user_query.split()
                if len(word) > 3
                and word.lower() not in ["the", "and", "for", "with", "that", "this"]
            ]
            context_keywords.extend(query_words)

        # Remove duplicates and empty strings
        context_keywords = list(set([kw for kw in context_keywords if kw]))

        # Time range analysis
        if "timestamp" in df.columns and not df["timestamp"].isna().all():
            timestamps = []
            for ts in df["timestamp"].dropna():
                try:
                    # Try different timestamp formats
                    for fmt in [
                        "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%dT%H:%M:%S",
                        "%m/%d/%Y %H:%M:%S",
                    ]:
                        try:
                            parsed_ts = datetime.strptime(str(ts), fmt)
                            timestamps.append(parsed_ts)
                            break
                        except ValueError:
                            continue
                except Exception:
                    continue

            if timestamps:
                insights["time_range"] = {
                    "start": min(timestamps).isoformat(),
                    "end": max(timestamps).isoformat(),
                    "duration_hours": (
                        max(timestamps) - min(timestamps)
                    ).total_seconds()
                    / 3600,
                }

        # Log level distribution
        if "log_level" in df.columns:
            level_counts = df["log_level"].value_counts().to_dict()
            insights["log_level_distribution"] = level_counts

        # Error analysis
        error_entries = df[df["log_level"].isin(["ERROR", "FATAL", "CRITICAL"])]
        insights["error_summary"] = {
            "total_errors": len(error_entries),
            "error_rate": len(error_entries) / len(df) if len(df) > 0 else 0,
        }

        # Performance metrics
        if "duration_ms" in df.columns:
            durations = df["duration_ms"].dropna()
            if len(durations) > 0:
                insights["performance_metrics"] = {
                    "avg_response_time_ms": durations.mean(),
                    "max_response_time_ms": durations.max(),
                    "min_response_time_ms": durations.min(),
                    "p95_response_time_ms": durations.quantile(0.95),
                }

        # HTTP status analysis
        if "http_status" in df.columns:
            status_counts = df["http_status"].value_counts().to_dict()
            insights["http_status_distribution"] = status_counts

        # Top errors
        if "error_code" in df.columns:
            error_codes = df["error_code"].value_counts().head(5).to_dict()
            insights["top_errors"] = list(error_codes.keys())

        # Unique IPs
        if "ip_address" in df.columns:
            insights["unique_ips"] = df["ip_address"].nunique()

        return insights
    
    def _detect_anomalies(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        """Detect anomalies in log data"""
        anomalies = []

        # Error rate anomalies
        if len(df) > 10:
            error_entries = df[df["log_level"].isin(["ERROR", "FATAL", "CRITICAL"])]
            error_rate = len(error_entries) / len(df)

            if error_rate > 0.1:  # More than 10% errors
                anomalies.append(
                    {
                        "type": "high_error_rate",
                        "severity": "high" if error_rate > 0.2 else "medium",
                        "description": f"Error rate is {error_rate:.2%}",
                        "value": error_rate,
                        "threshold": 0.1,
                    }
                )

        # Performance anomalies
        if "duration_ms" in df.columns:
            durations = df["duration_ms"].dropna()
            if len(durations) > 5:
                try:
                    scaler = StandardScaler()
                    scaled_durations = scaler.fit_transform(
                        durations.values.reshape(-1, 1)
                    )

                    iso_forest = IsolationForest(contamination=0.1, random_state=42)
                    predictions = iso_forest.fit_predict(scaled_durations)

                    outlier_indices = np.where(predictions == -1)[0]
                    if len(outlier_indices) > 0:
                        outlier_durations = durations.iloc[outlier_indices]

                        for idx, duration in outlier_durations.items():
                            anomalies.append(
                                {
                                    "type": "performance_outlier",
                                    "severity": (
                                        "high"
                                        if duration > durations.quantile(0.95)
                                        else "medium"
                                    ),
                                    "description": f"Unusually slow response: {duration:.2f}ms",
                                    "value": duration,
                                    "line_number": (
                                        str(df.iloc[idx]["line_number"])
                                        if "line_number" in df.columns
                                        else "unknown"
                                    ),
                                }
                            )
                except Exception as e:
                    self.logger.warning(f"Performance anomaly detection failed: {e}")

        return anomalies
    
    def _generate_recommendations(
        self,
        insights: Dict[str, Any],
        anomalies: List[Dict[str, Any]],
        agent_state: Dict[str, Any],
    ) -> List[str]:
        """Generate recommendations based on insights and anomalies"""
        recommendations = []
        
        # Error rate recommendations
        if insights.get("error_summary", {}).get("error_rate", 0) > 0.1:
            recommendations.append(
                "High error rate detected. Review application logs for root causes."
            )

        # Performance recommendations
        perf_metrics = insights.get("performance_metrics", {})
        if perf_metrics.get("avg_response_time_ms", 0) > 1000:
            recommendations.append(
                "Average response time is high. Consider performance optimization."
            )

        # Anomaly-based recommendations
        for anomaly in anomalies:
            if anomaly["type"] == "high_error_rate":
                recommendations.append("Investigate the high error rate immediately.")
            elif anomaly["type"] == "performance_outlier":
                recommendations.append(
                    "Review the identified slow requests for optimization."
                )

        return recommendations
    
    def _calculate_confidence(
        self,
        df: pd.DataFrame,
        insights: Dict[str, Any],
        anomalies: List[Dict[str, Any]],
    ) -> float:
        """Calculate confidence score for the analysis"""
        confidence = 0.5  # Base confidence

        # Increase confidence based on data quality
        if len(df) > 100:
            confidence += 0.2
        elif len(df) > 10:
            confidence += 0.1

        # Increase confidence if we have timestamps
        if insights.get("time_range"):
            confidence += 0.1

        # Increase confidence if we have log levels
        if insights.get("log_level_distribution"):
            confidence += 0.1

        # Increase confidence if we have performance metrics
        if insights.get("performance_metrics"):
            confidence += 0.1

        return min(1.0, max(0.0, confidence))
    
    # Legacy interface compatibility
    @trace("enhanced_log_processor_process_legacy")
    async def process(self, content: str, data_type: Optional[DataType] = None) -> Dict[str, Any]:
        """
        Legacy processing method for backward compatibility
        
        Args:
            content: Raw log content
            data_type: Optional data type for processing hints
            
        Returns:
            Dictionary with extracted insights (legacy format)
        """
        # Use enhanced processing but return simplified format
        result = await self.process_with_context(
            content=content,
            session_id="legacy",
            data_type=data_type,
            context=None
        )
        
        # Convert to legacy format
        return {
            **result.insights,
            "anomalies": result.anomalies,
            "processing_time_ms": result.processing_time_ms,
            "confidence_score": result.confidence_score
        }


class LogProcessor(ILogProcessor):
    """Processes log files to extract insights and detect anomalies with context-aware analysis"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

        # Common log patterns for parsing
        self.log_patterns = {
            "timestamp": [
                r"(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)",
                r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})",
                r"(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2})",
            ],
            "log_level": [
                r"\b(ERROR|WARN|WARNING|INFO|DEBUG|FATAL|CRITICAL)\b",
            ],
            "http_status": [
                r"\b(2\d{2}|3\d{2}|4\d{2}|5\d{2})\b",
            ],
            "ip_address": [
                r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
            ],
            "error_code": [
                r"\b[A-Z_]+_ERROR\b",
                r"\b[A-Z_]+_EXCEPTION\b",
            ],
            "duration": [
                r"(\d+(?:\.\d+)?)\s*(?:ms|s|seconds?)",
            ],
        }

        # Compile patterns
        self.compiled_patterns = {}
        for category, patterns in self.log_patterns.items():
            self.compiled_patterns[category] = [
                re.compile(pattern, re.IGNORECASE) for pattern in patterns
            ]

    @trace("log_processor_process")
    async def process(self, content: str, data_type: Optional[DataType] = None) -> Dict[str, Any]:
        """
        Process log content and extract insights (interface-compliant method)

        Args:
            content: Raw log content
            data_type: Optional data type for processing hints

        Returns:
            Dictionary with extracted insights
        """
        try:
            # Parse logs into structured format
            df = self._parse_logs_to_dataframe(content)

            if df.empty:
                return {
                    "error": "No valid log entries found",
                    "total_entries": 0,
                    "processing_error": True
                }

            # Create mock agent state for context-aware processing
            mock_agent_state = {
                "user_query": "",
                "investigation_context": {},
                "current_phase": "analyze"
            }

            # Extract basic insights
            insights = self._extract_basic_insights(df, mock_agent_state)

            # Detect anomalies
            anomalies = self._detect_anomalies(df)

            # Add anomalies to insights
            insights["anomalies"] = anomalies

            # Return simplified insights
            return insights

        except Exception as e:
            self.logger.error(f"Log processing failed: {e}")
            return {
                "error": str(e),
                "processing_error": True,
                "total_entries": 0
            }

    @trace("log_processor_process_detailed")
    async def process_detailed(
        self, content: str, data_id: str, agent_state: AgentState
    ) -> DataInsightsResponse:
        """
        Process log content and extract insights with context-aware analysis (legacy method)

        Args:
            content: Raw log content
            data_id: Identifier for the data
            agent_state: Current agent state for context-aware processing

        Returns:
            DataInsightsResponse with extracted insights
        """
        start_time = datetime.utcnow()

        try:
            # Parse logs into structured format
            df = self._parse_logs_to_dataframe(content)

            if df.empty:
                return DataInsightsResponse(
                    data_id=data_id,
                    data_type=DataType.LOG_FILE,
                    insights={"error": "No valid log entries found"},
                    confidence_score=0.0,
                    processing_time_ms=0,
                    anomalies_detected=[],
                    recommendations=[],
                )

            # Extract basic insights with context awareness
            insights = self._extract_basic_insights(df, agent_state)

            # Detect anomalies
            anomalies = self._detect_anomalies(df)

            # Generate context-aware recommendations
            recommendations = self._generate_recommendations(
                insights, anomalies, agent_state
            )

            # Calculate confidence score
            confidence = self._calculate_confidence(df, insights, anomalies)

            # Calculate processing time
            processing_time = int(
                (datetime.utcnow() - start_time).total_seconds() * 1000
            )

            return DataInsightsResponse(
                data_id=data_id,
                data_type=DataType.LOG_FILE,
                insights=insights,
                confidence_score=confidence,
                processing_time_ms=processing_time,
                anomalies_detected=anomalies,
                recommendations=recommendations,
            )

        except Exception as e:
            self.logger.error(f"Log processing failed: {e}")
            return DataInsightsResponse(
                data_id=data_id,
                data_type=DataType.LOG_FILE,
                insights={"error": str(e)},
                confidence_score=0.0,
                processing_time_ms=0,
                anomalies_detected=[],
                recommendations=[],
            )

    def _parse_logs_to_dataframe(self, content: str) -> pd.DataFrame:
        """
        Parse unstructured log content into a structured DataFrame

        Args:
            content: Raw log content

        Returns:
            DataFrame with parsed log entries
        """
        lines = content.strip().split("\n")
        parsed_entries = []

        for line_num, line in enumerate(lines, 1):
            if not line.strip():
                continue

            entry = self._parse_log_line(line, line_num)
            if entry:
                parsed_entries.append(entry)

        return pd.DataFrame(parsed_entries)

    def _parse_log_line(self, line: str, line_num: int) -> Optional[Dict[str, Any]]:
        """
        Parse a single log line

        Args:
            line: Single log line
            line_num: Line number for reference

        Returns:
            Parsed log entry dictionary
        """
        # Safety check for None line
        if line is None:
            return None

        entry = {
            "line_number": line_num,
            "raw_line": line,
            "timestamp": None,
            "log_level": None,
            "message": line,
            "http_status": None,
            "ip_address": None,
            "error_code": None,
            "duration_ms": None,
        }

        # Extract timestamp
        for pattern in self.compiled_patterns["timestamp"]:
            match = pattern.search(line)
            if match:
                entry["timestamp"] = match.group(1)
                break

        # Extract log level
        for pattern in self.compiled_patterns["log_level"]:
            match = pattern.search(line)
            if match:
                entry["log_level"] = match.group(1).upper()
                break

        # Extract HTTP status codes
        for pattern in self.compiled_patterns["http_status"]:
            match = pattern.search(line)
            if match:
                entry["http_status"] = int(match.group(1))
                break

        # Extract IP addresses
        for pattern in self.compiled_patterns["ip_address"]:
            match = pattern.search(line)
            if match:
                entry["ip_address"] = match.group(0)
                break

        # Extract error codes
        for pattern in self.compiled_patterns["error_code"]:
            match = pattern.search(line)
            if match:
                entry["error_code"] = match.group(0)
                break

        # Extract duration
        for pattern in self.compiled_patterns["duration"]:
            match = pattern.search(line)
            if match:
                duration_str = match.group(1)
                try:
                    duration = float(duration_str)
                    if "ms" in line.lower():
                        entry["duration_ms"] = duration
                    else:
                        entry["duration_ms"] = duration * 1000  # Convert to ms
                except ValueError:
                    pass
                break

        return entry

    def _extract_basic_insights(
        self, df: pd.DataFrame, agent_state: AgentState
    ) -> Dict[str, Any]:
        """
        Extract basic insights from parsed log data with context awareness

        Args:
            df: Parsed log DataFrame
            agent_state: Current agent state for context-aware processing

        Returns:
            Dictionary of insights
        """
        insights: Dict[str, Any] = {
            "total_entries": len(df),
            "time_range": None,
            "log_level_distribution": {},
            "error_summary": {},
            "performance_metrics": {},
            "top_errors": [],
            "unique_ips": 0,
            "contextual_analysis": {},
        }

        # Extract context keywords from agent state
        context_keywords = []
        investigation_context = agent_state.get("investigation_context", {})

        # Get keywords from various context sources
        if "keywords" in investigation_context:
            context_keywords.extend(investigation_context["keywords"])
        if "services" in investigation_context:
            context_keywords.extend(investigation_context["services"])
        if "components" in investigation_context:
            context_keywords.extend(investigation_context["components"])

        # Extract keywords from user query
        user_query = agent_state.get("user_query", "")
        if user_query:
            # Simple keyword extraction from user query
            query_words = [
                word.lower()
                for word in user_query.split()
                if len(word) > 3
                and word.lower() not in ["the", "and", "for", "with", "that", "this"]
            ]
            context_keywords.extend(query_words)

        # Remove duplicates and empty strings
        context_keywords = list(set([kw for kw in context_keywords if kw]))

        # Time range analysis
        if "timestamp" in df.columns and not df["timestamp"].isna().all():
            timestamps = []
            for ts in df["timestamp"].dropna():
                try:
                    # Try different timestamp formats
                    for fmt in [
                        "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%dT%H:%M:%S",
                        "%m/%d/%Y %H:%M:%S",
                    ]:
                        try:
                            parsed_ts = datetime.strptime(str(ts), fmt)
                            timestamps.append(parsed_ts)
                            break
                        except ValueError:
                            continue
                except Exception:
                    continue

            if timestamps:
                insights["time_range"] = {
                    "start": min(timestamps).isoformat(),
                    "end": max(timestamps).isoformat(),
                    "duration_hours": (
                        max(timestamps) - min(timestamps)
                    ).total_seconds()
                    / 3600,
                }

        # Log level distribution
        if "log_level" in df.columns:
            level_counts = df["log_level"].value_counts().to_dict()
            insights["log_level_distribution"] = level_counts

        # Context-aware error analysis
        error_entries = df[df["log_level"].isin(["ERROR", "FATAL", "CRITICAL"])]
        insights["error_summary"] = {
            "total_errors": len(error_entries),
            "error_rate": len(error_entries) / len(df) if len(df) > 0 else 0,
        }

        # Contextual analysis - prioritize logs matching investigation context
        if context_keywords and "raw_line" in df.columns:
            # Create a case-insensitive regex pattern for context keywords
            context_pattern = "|".join(re.escape(kw) for kw in context_keywords)
            context_mask = df["raw_line"].str.contains(
                context_pattern, case=False, na=False
            )
            contextual_logs = df[context_mask]

            insights["contextual_analysis"] = {
                "context_keywords": context_keywords,
                "contextual_entries": len(contextual_logs),
                "contextual_percentage": (
                    len(contextual_logs) / len(df) * 100 if len(df) > 0 else 0
                ),
            }

            # Prioritize contextual errors
            contextual_errors = contextual_logs[
                contextual_logs["log_level"].isin(["ERROR", "FATAL", "CRITICAL"])
            ]
            if len(contextual_errors) > 0:
                insights["contextual_analysis"]["contextual_errors"] = len(
                    contextual_errors
                )
                insights["contextual_analysis"]["contextual_error_rate"] = (
                    len(contextual_errors) / len(contextual_logs) * 100
                )

                # Extract top contextual error messages
                insights["contextual_analysis"]["top_contextual_errors"] = (
                    contextual_errors["raw_line"].head(5).tolist()
                )

            # Contextual performance analysis
            if "duration_ms" in contextual_logs.columns:
                contextual_durations = contextual_logs["duration_ms"].dropna()
                if len(contextual_durations) > 0:
                    insights["contextual_analysis"]["contextual_performance"] = {
                        "avg_response_time_ms": contextual_durations.mean(),
                        "max_response_time_ms": contextual_durations.max(),
                        "count": len(contextual_durations),
                    }

        # HTTP status analysis
        if "http_status" in df.columns:
            status_counts = df["http_status"].value_counts().to_dict()
            insights["http_status_distribution"] = status_counts

        # Performance metrics
        if "duration_ms" in df.columns:
            durations = df["duration_ms"].dropna()
            if len(durations) > 0:
                insights["performance_metrics"] = {
                    "avg_response_time_ms": durations.mean(),
                    "max_response_time_ms": durations.max(),
                    "min_response_time_ms": durations.min(),
                    "p95_response_time_ms": durations.quantile(0.95),
                }

        # Top errors
        if "error_code" in df.columns:
            error_codes = df["error_code"].value_counts().head(5).to_dict()
            insights["top_errors"] = list(error_codes.keys())

        # Unique IPs
        if "ip_address" in df.columns:
            insights["unique_ips"] = df["ip_address"].nunique()

        return insights

    def _detect_anomalies(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        Detect anomalies in log data

        Args:
            df: Parsed log DataFrame

        Returns:
            List of detected anomalies
        """
        anomalies = []

        # 1. Error rate anomalies
        if len(df) > 10:
            error_entries = df[df["log_level"].isin(["ERROR", "FATAL", "CRITICAL"])]
            error_rate = len(error_entries) / len(df)

            if error_rate > 0.1:  # More than 10% errors
                anomalies.append(
                    {
                        "type": "high_error_rate",
                        "severity": "high" if error_rate > 0.2 else "medium",
                        "description": f"Error rate is {error_rate:.2%}",
                        "value": error_rate,
                        "threshold": 0.1,
                    }
                )

        # 2. Performance anomalies
        if "duration_ms" in df.columns:
            durations = df["duration_ms"].dropna()
            if len(durations) > 5:
                # Use Isolation Forest for outlier detection
                try:
                    scaler = StandardScaler()
                    scaled_durations = scaler.fit_transform(
                        durations.values.reshape(-1, 1)
                    )

                    iso_forest = IsolationForest(contamination=0.1, random_state=42)
                    predictions = iso_forest.fit_predict(scaled_durations)

                    outlier_indices = np.where(predictions == -1)[0]
                    if len(outlier_indices) > 0:
                        outlier_durations = durations.iloc[outlier_indices]

                        for idx, duration in outlier_durations.items():
                            anomalies.append(
                                {
                                    "type": "performance_outlier",
                                    "severity": (
                                        "high"
                                        if duration > durations.quantile(0.95)
                                        else "medium"
                                    ),
                                    "description": f"Unusually slow response: {duration:.2f}ms",
                                    "value": duration,
                                    "line_number": (
                                        str(df.iloc[idx]["line_number"])
                                        if "line_number" in df.columns
                                        else "unknown"
                                    ),
                                }
                            )
                except Exception as e:
                    self.logger.warning(f"Performance anomaly detection failed: {e}")

        # 3. HTTP status anomalies
        if "http_status" in df.columns:
            status_counts = df["http_status"].value_counts()
            error_statuses = status_counts[status_counts.index >= 400]

            for status, count in error_statuses.items():
                if count > len(df) * 0.05:  # More than 5% of requests
                    anomalies.append(
                        {
                            "type": "http_error_spike",
                            "severity": "high" if status >= 500 else "medium",
                            "description": f"High rate of HTTP {status} errors: {count} occurrences",
                            "value": count,
                            "status_code": status,
                        }
                    )

        # 4. Temporal anomalies (if timestamps available)
        if "timestamp" in df.columns and not df["timestamp"].isna().all():
            # Simple temporal clustering for now
            # In a real implementation, you might use more sophisticated time series analysis
            pass

        return anomalies

    def _generate_recommendations(
        self,
        insights: Dict[str, Any],
        anomalies: List[Dict[str, Any]],
        agent_state: AgentState,
    ) -> List[str]:
        """
        Generate context-aware recommendations based on insights, anomalies, and agent state

        Args:
            insights: Extracted insights
            anomalies: Detected anomalies
            agent_state: Current agent state for context-aware recommendations

        Returns:
            List of recommendations
        """
        recommendations = []
        current_phase = agent_state.get("current_phase", "")
        investigation_context = agent_state.get("investigation_context", {})

        # Phase-specific recommendations
        if current_phase == "define_blast_radius":
            if insights.get("contextual_analysis", {}).get("contextual_entries", 0) > 0:
                contextual_pct = insights["contextual_analysis"][
                    "contextual_percentage"
                ]
                recommendations.append(
                    f"Blast radius analysis: {contextual_pct:.1f}% of log entries are related to your investigation context. "
                    f"Focus on these {insights['contextual_analysis']['contextual_entries']} entries."
                )

            # Time range for blast radius
            if insights.get("time_range"):
                recommendations.append(
                    f"Time range affected: {insights['time_range']['start']} to {insights['time_range']['end']} "
                    f"({insights['time_range']['duration_hours']:.1f} hours)"
                )

        elif current_phase == "establish_timeline":
            if insights.get("time_range"):
                recommendations.append(
                    f"Timeline established: Logs span from {insights['time_range']['start']} to {insights['time_range']['end']}. "
                    "Correlate this with deployment events, configuration changes, or external incidents."
                )

            # Contextual timeline analysis
            contextual_analysis = insights.get("contextual_analysis", {})
            if contextual_analysis.get("contextual_entries", 0) > 0:
                recommendations.append(
                    f"Found {contextual_analysis['contextual_entries']} relevant log entries. "
                    "Review these for timeline correlation with the reported issue."
                )

        elif current_phase == "formulate_hypothesis":
            # Context-aware hypothesis suggestions
            contextual_analysis = insights.get("contextual_analysis", {})
            if contextual_analysis.get("contextual_errors", 0) > 0:
                recommendations.append(
                    f"Hypothesis: {contextual_analysis['contextual_errors']} errors found in context-relevant logs. "
                    "This suggests the issue is related to the components you're investigating."
                )

            # Performance-based hypothesis
            if contextual_analysis.get("contextual_performance"):
                avg_time = contextual_analysis["contextual_performance"][
                    "avg_response_time_ms"
                ]
                max_time = contextual_analysis["contextual_performance"][
                    "max_response_time_ms"
                ]
                recommendations.append(
                    f"Hypothesis: Performance degradation detected (avg: {avg_time:.0f}ms, max: {max_time:.0f}ms). "
                    "Consider resource constraints or downstream dependencies."
                )

        elif current_phase == "validate_hypothesis":
            # Provide validation guidance based on context
            contextual_analysis = insights.get("contextual_analysis", {})
            if contextual_analysis.get("top_contextual_errors"):
                recommendations.append(
                    "Validation: Review these specific error messages for hypothesis confirmation:"
                )
                for i, error in enumerate(
                    contextual_analysis["top_contextual_errors"][:3], 1
                ):
                    recommendations.append(f"  {i}. {error[:100]}...")

        elif current_phase == "propose_solution":
            # Solution-oriented recommendations
            error_rate = insights.get("error_summary", {}).get("error_rate", 0)
            if error_rate > 0.1:
                recommendations.append(
                    f"Solution: Address the {error_rate:.1%} error rate. "
                    "Consider implementing circuit breakers, retry mechanisms, or scaling resources."
                )

            # Context-specific solutions
            contextual_analysis = insights.get("contextual_analysis", {})
            if contextual_analysis.get("contextual_errors", 0) > 0:
                recommendations.append(
                    "Solution: Focus remediation efforts on the context-relevant errors identified. "
                    "These are most likely related to the reported issue."
                )

        # General error rate recommendations
        if insights.get("error_summary", {}).get("error_rate", 0) > 0.1:
            recommendations.append(
                "High error rate detected. Review application logs for root causes."
            )

        # Performance recommendations
        perf_metrics = insights.get("performance_metrics", {})
        if perf_metrics.get("avg_response_time_ms", 0) > 1000:
            recommendations.append(
                "Average response time is high. Consider performance optimization."
            )

        # Anomaly-based recommendations
        for anomaly in anomalies:
            if anomaly["type"] == "high_error_rate":
                recommendations.append("Investigate the high error rate immediately.")
            elif anomaly["type"] == "performance_outlier":
                recommendations.append(
                    "Review the identified slow requests for optimization."
                )
            elif anomaly["type"] == "http_error_spike":
                recommendations.append(
                    f"Investigate HTTP {anomaly.get('status_code')} errors."
                )

        # Context-aware general recommendations
        contextual_analysis = insights.get("contextual_analysis", {})
        if contextual_analysis.get(
            "contextual_entries", 0
        ) == 0 and contextual_analysis.get("context_keywords"):
            recommendations.append(
                f"No logs found matching your investigation context ({', '.join(contextual_analysis['context_keywords'])}). "
                "Consider expanding the search scope or reviewing log completeness."
            )

        # Default recommendation if no issues found
        if (
            len(anomalies) == 0
            and insights.get("error_summary", {}).get("error_rate", 0) < 0.05
        ):
            recommendations.append("No significant anomalies detected in the logs.")

        return recommendations

    def _calculate_confidence(
        self,
        df: pd.DataFrame,
        insights: Dict[str, Any],
        anomalies: List[Dict[str, Any]],
    ) -> float:
        """
        Calculate confidence score for the analysis

        Args:
            df: Parsed log DataFrame
            insights: Extracted insights
            anomalies: Detected anomalies

        Returns:
            Confidence score between 0.0 and 1.0
        """
        confidence = 0.5  # Base confidence

        # Increase confidence based on data quality
        if len(df) > 100:
            confidence += 0.2
        elif len(df) > 10:
            confidence += 0.1

        # Increase confidence if we have timestamps
        if insights.get("time_range"):
            confidence += 0.1

        # Increase confidence if we have log levels
        if insights.get("log_level_distribution"):
            confidence += 0.1

        # Increase confidence if we have performance metrics
        if insights.get("performance_metrics"):
            confidence += 0.1

        # Decrease confidence if we have many anomalies (might indicate parsing issues)
        if len(anomalies) > 10:
            confidence -= 0.1

        return min(1.0, max(0.0, confidence))
