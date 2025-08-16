"""Enhanced Data Service - Phase 3

Purpose: Memory-aware data processing with pattern learning and context integration

This enhanced service builds upon the existing DataService to provide:
- Memory-enhanced data classification and processing
- Pattern learning from user feedback and interactions
- Context-aware processing optimizations
- Enhanced security assessment with learned patterns
- Integration with memory service for historical context

Core Responsibilities:
- Memory-enhanced data ingestion and validation
- Context-aware data type classification
- Pattern learning and application
- Enhanced security assessment
- Intelligent insight extraction with memory context
- Cross-session pattern sharing and learning

Key Enhancements:
- Memory service integration for context awareness
- Pattern learner for continuous improvement
- Enhanced classifier and processor integration
- Adaptive processing based on user feedback
- Security pattern learning and application
"""

import hashlib
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

from faultmaven.services.base_service import BaseService
from faultmaven.models.interfaces import (
    IDataClassifier, ILogProcessor, ISanitizer, ITracer, IStorageBackend, IMemoryService
)
from faultmaven.models import (
    DataInsightsResponse,
    DataType,
    UploadedData,
)
from faultmaven.exceptions import ValidationException, ServiceException

# Import enhanced components
from faultmaven.core.processing.classifier import EnhancedDataClassifier, ClassificationResult
from faultmaven.core.processing.log_analyzer import EnhancedLogProcessor, EnhancedProcessingResult
from faultmaven.core.processing.pattern_learner import PatternLearner, PatternType, LearningResult


@dataclass
class EnhancedIngestionResult:
    """Enhanced result from data ingestion with memory and learning information"""
    data_id: str
    session_id: str
    data_type: str
    content: str
    file_name: str
    file_size: int
    processing_status: str
    classification_result: ClassificationResult
    processing_result: Optional[EnhancedProcessingResult]
    insights: Dict[str, Any]
    memory_enhanced: bool
    patterns_applied: List[str]
    learning_opportunities: List[str]
    processing_time_ms: float


class EnhancedDataService(BaseService):
    """
    Enhanced Data Service with memory integration and pattern learning
    
    This service extends the standard data processing capabilities with:
    - Memory-aware classification and processing
    - Pattern learning from user interactions
    - Context-driven processing optimizations
    - Enhanced security assessment
    - Cross-session learning and improvement
    """
    
    def __init__(
        self,
        memory_service: IMemoryService,
        data_classifier: Optional[IDataClassifier] = None,
        log_processor: Optional[ILogProcessor] = None,
        sanitizer: Optional[ISanitizer] = None,
        tracer: Optional[ITracer] = None,
        storage_backend: Optional[IStorageBackend] = None,
        session_service=None,
        pattern_learner: Optional[PatternLearner] = None
    ):
        """
        Initialize Enhanced Data Service with memory and learning capabilities
        
        Args:
            memory_service: Memory service for context retrieval and storage
            data_classifier: Enhanced data classifier (optional, will create if not provided)
            log_processor: Enhanced log processor (optional, will create if not provided)
            sanitizer: Data sanitization service interface
            tracer: Distributed tracing interface
            storage_backend: Optional storage backend interface
            session_service: Optional session service for operation tracking
            pattern_learner: Pattern learning service (optional, will create if not provided)
        """
        super().__init__()
        
        # Core services
        self._memory_service = memory_service
        self._sanitizer = sanitizer
        self._tracer = tracer
        self._storage = storage_backend
        self._session_service = session_service
        
        # Enhanced processing components
        self._enhanced_classifier = data_classifier or EnhancedDataClassifier(memory_service)
        self._enhanced_processor = log_processor or EnhancedLogProcessor(memory_service)
        self._pattern_learner = pattern_learner or PatternLearner(memory_service)
        
        # Performance metrics
        self._metrics = {
            "enhanced_ingestions": 0,
            "memory_enhanced_operations": 0,
            "patterns_applied": 0,
            "learning_sessions": 0,
            "avg_processing_time": 0.0,
            "avg_classification_confidence": 0.0,
            "avg_context_relevance": 0.0
        }
        
        # Processing history for learning
        self._processing_history = []
        
    async def ingest_data_enhanced(
        self,
        content: str,
        session_id: str,
        file_name: Optional[str] = None,
        file_size: Optional[int] = None,
        data_type: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> EnhancedIngestionResult:
        """
        Enhanced data ingestion with memory integration and pattern learning
        
        This method provides comprehensive data processing with:
        - Memory-aware classification
        - Context-driven processing
        - Pattern application and learning
        - Enhanced security assessment
        
        Args:
            content: Raw data content
            session_id: Session identifier for memory context
            file_name: Optional original filename
            file_size: Optional file size in bytes
            data_type: Optional data type override
            context: Optional additional context data
            
        Returns:
            EnhancedIngestionResult with comprehensive processing information
            
        Raises:
            ValidationException: If input validation fails
            ServiceException: If processing fails
        """
        start_time = time.time()
        
        # Validate inputs
        await self._validate_enhanced_ingestion_inputs(
            content, session_id, file_name, file_size, data_type, context
        )
        
        try:
            # Generate data ID
            data_id = self._generate_data_id(content)
            
            # Log enhanced business event
            self.log_business_event(
                "enhanced_data_ingestion_started",
                "info",
                {
                    "data_id": data_id,
                    "session_id": session_id,
                    "file_name": file_name,
                    "content_size": len(content),
                    "memory_enhanced": True,
                    "pattern_learning_enabled": True
                }
            )
            
            # Sanitize content
            sanitized_content = self._sanitizer.sanitize(content) if self._sanitizer else content
            
            # Enhanced classification with memory context
            classification_result = await self._enhanced_classifier.classify_with_context(
                content=sanitized_content,
                session_id=session_id,
                filename=file_name,
                context=context
            )
            
            # Apply learned patterns for enhanced classification
            classification_patterns = await self._pattern_learner.apply_patterns(
                content=sanitized_content,
                pattern_type=PatternType.CLASSIFICATION,
                context=context
            )
            
            # Determine final data type
            final_data_type = data_type or classification_result.data_type.value
            
            # Enhanced processing based on data type
            processing_result = None
            if classification_result.data_type in [DataType.LOG_FILE, DataType.ERROR_MESSAGE]:
                processing_result = await self._enhanced_processor.process_with_context(
                    content=sanitized_content,
                    session_id=session_id,
                    data_type=classification_result.data_type,
                    context=context
                )
            
            # Apply security patterns
            security_patterns = await self._pattern_learner.apply_patterns(
                content=sanitized_content,
                pattern_type=PatternType.SECURITY,
                context=context
            )
            
            # Combine insights from all processing
            combined_insights = self._combine_processing_insights(
                classification_result, processing_result, classification_patterns, security_patterns
            )
            
            # Identify learning opportunities
            learning_opportunities = self._identify_learning_opportunities(
                classification_result, processing_result, context
            )
            
            # Create enhanced result
            enhanced_result = EnhancedIngestionResult(
                data_id=data_id,
                session_id=session_id,
                data_type=final_data_type,
                content=sanitized_content,
                file_name=file_name or "unknown",
                file_size=file_size or len(content),
                processing_status="completed",
                classification_result=classification_result,
                processing_result=processing_result,
                insights=combined_insights,
                memory_enhanced=classification_result.memory_enhanced or (
                    processing_result.memory_enhanced if processing_result else False
                ),
                patterns_applied=[
                    f"classification_patterns_{len(classification_patterns.get('matches', []))}",
                    f"security_patterns_{len(security_patterns.get('matches', []))}"
                ],
                learning_opportunities=learning_opportunities,
                processing_time_ms=(time.time() - start_time) * 1000
            )
            
            # Store enhanced result
            if self._storage:
                await self._storage.store(data_id, enhanced_result)
            
            # Update metrics
            self._update_enhanced_metrics(enhanced_result)
            
            # Store processing for future learning
            self._processing_history.append({
                "data_id": data_id,
                "session_id": session_id,
                "result": enhanced_result,
                "timestamp": time.time()
            })
            
            # Record operation in session service
            if self._session_service:
                await self._record_enhanced_operation(enhanced_result)
            
            # Log completion
            self.log_business_event(
                "enhanced_data_ingestion_completed",
                "info",
                {
                    "data_id": data_id,
                    "session_id": session_id,
                    "processing_time_ms": enhanced_result.processing_time_ms,
                    "memory_enhanced": enhanced_result.memory_enhanced,
                    "patterns_applied": len(enhanced_result.patterns_applied),
                    "learning_opportunities": len(enhanced_result.learning_opportunities),
                    "classification_confidence": classification_result.confidence,
                    "context_relevance": classification_result.context_relevance
                }
            )
            
            return enhanced_result
            
        except Exception as e:
            self.logger.error(f"Enhanced data ingestion failed for session {session_id}: {e}")
            self.log_business_event(
                "enhanced_data_ingestion_failed",
                "error",
                {
                    "session_id": session_id,
                    "error": str(e),
                    "processing_time_ms": (time.time() - start_time) * 1000
                }
            )
            raise ServiceException(
                f"Enhanced data ingestion failed: {str(e)}",
                details={"session_id": session_id, "error": str(e)}
            ) from e
    
    async def learn_from_feedback(
        self,
        data_id: str,
        session_id: str,
        user_feedback: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> LearningResult:
        """
        Learn from user feedback to improve future processing
        
        Args:
            data_id: Identifier of the data that was processed
            session_id: Session identifier
            user_feedback: User feedback and corrections
            context: Optional additional context
            
        Returns:
            LearningResult with learning outcome details
        """
        try:
            # Find the original processing result
            original_processing = None
            for entry in self._processing_history:
                if entry["data_id"] == data_id and entry["session_id"] == session_id:
                    original_processing = entry
                    break
            
            if not original_processing:
                raise ValidationException(f"No processing history found for data {data_id}")
            
            # Extract predicted and actual results for learning
            predicted_result = {
                "data_type": original_processing["result"].data_type,
                "classification": original_processing["result"].classification_result,
                "processing": original_processing["result"].processing_result
            }
            
            actual_result = user_feedback.get("corrections", {})
            
            # Delegate to pattern learner
            learning_result = await self._pattern_learner.learn_from_feedback(
                content=original_processing["result"].content,
                predicted_result=predicted_result,
                actual_result=actual_result,
                user_feedback=user_feedback,
                session_id=session_id,
                context=context
            )
            
            # Update metrics
            self._metrics["learning_sessions"] += 1
            
            # Log learning session
            self.log_business_event(
                "pattern_learning_completed",
                "info",
                {
                    "data_id": data_id,
                    "session_id": session_id,
                    "patterns_learned": learning_result.patterns_learned,
                    "patterns_updated": learning_result.patterns_updated,
                    "learning_confidence": learning_result.learning_confidence
                }
            )
            
            return learning_result
            
        except Exception as e:
            self.logger.error(f"Learning from feedback failed: {e}")
            raise ServiceException(
                f"Pattern learning failed: {str(e)}",
                details={"data_id": data_id, "session_id": session_id}
            ) from e
    
    async def get_processing_insights(
        self,
        session_id: str,
        data_type_filter: Optional[str] = None,
        time_range_hours: Optional[int] = 24
    ) -> Dict[str, Any]:
        """
        Get processing insights and patterns for a session
        
        Args:
            session_id: Session identifier
            data_type_filter: Optional filter by data type
            time_range_hours: Time range for analysis in hours
            
        Returns:
            Dictionary with processing insights and recommendations
        """
        try:
            # Filter processing history
            cutoff_time = time.time() - (time_range_hours * 3600) if time_range_hours else 0
            filtered_history = [
                entry for entry in self._processing_history
                if (entry["session_id"] == session_id and 
                    entry["timestamp"] > cutoff_time and
                    (not data_type_filter or entry["result"].data_type == data_type_filter))
            ]
            
            if not filtered_history:
                return {"message": "No processing history found for the specified criteria"}
            
            # Analyze processing patterns
            insights = {
                "total_processed": len(filtered_history),
                "data_types": {},
                "classification_accuracy": 0.0,
                "memory_enhancement_rate": 0.0,
                "pattern_utilization": {},
                "learning_opportunities": [],
                "recommendations": []
            }
            
            # Data type distribution
            type_counts = {}
            total_confidence = 0.0
            memory_enhanced_count = 0
            
            for entry in filtered_history:
                result = entry["result"]
                data_type = result.data_type
                
                if data_type not in type_counts:
                    type_counts[data_type] = 0
                type_counts[data_type] += 1
                
                total_confidence += result.classification_result.confidence
                
                if result.memory_enhanced:
                    memory_enhanced_count += 1
                
                # Collect learning opportunities
                insights["learning_opportunities"].extend(result.learning_opportunities)
            
            insights["data_types"] = type_counts
            insights["classification_accuracy"] = total_confidence / len(filtered_history)
            insights["memory_enhancement_rate"] = memory_enhanced_count / len(filtered_history)
            
            # Get pattern statistics
            pattern_stats = self._pattern_learner.get_pattern_statistics()
            insights["pattern_utilization"] = pattern_stats
            
            # Generate recommendations
            insights["recommendations"] = self._generate_processing_recommendations(
                insights, filtered_history
            )
            
            return insights
            
        except Exception as e:
            self.logger.error(f"Failed to get processing insights: {e}")
            return {"error": str(e)}
    
    async def _validate_enhanced_ingestion_inputs(
        self,
        content: str,
        session_id: str,
        file_name: Optional[str],
        file_size: Optional[int],
        data_type: Optional[str],
        context: Optional[Dict[str, Any]]
    ) -> None:
        """Validate inputs for enhanced ingestion"""
        if not content or not content.strip():
            raise ValidationException("Content cannot be empty")
        
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
        
        if len(content) > 10 * 1024 * 1024:  # 10MB limit
            raise ValidationException("Content size exceeds maximum limit (10MB)")
        
        if data_type and data_type not in [dt.value for dt in DataType]:
            raise ValidationException(f"Invalid data type: {data_type}")
    
    def _combine_processing_insights(
        self,
        classification_result: ClassificationResult,
        processing_result: Optional[EnhancedProcessingResult],
        classification_patterns: Dict[str, Any],
        security_patterns: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Combine insights from all processing components"""
        combined = {
            "classification": {
                "data_type": classification_result.data_type.value,
                "confidence": classification_result.confidence,
                "context_relevance": classification_result.context_relevance,
                "pattern_matches": classification_result.pattern_matches,
                "security_flags": classification_result.security_flags,
                "memory_enhanced": classification_result.memory_enhanced
            },
            "patterns": {
                "classification_patterns": classification_patterns,
                "security_patterns": security_patterns
            }
        }
        
        if processing_result:
            combined["processing"] = {
                "insights": processing_result.insights,
                "anomalies": processing_result.anomalies,
                "recommendations": processing_result.recommendations,
                "confidence_score": processing_result.confidence_score,
                "context_relevance": processing_result.context_relevance,
                "security_flags": processing_result.security_flags,
                "memory_enhanced": processing_result.memory_enhanced
            }
        
        return combined
    
    def _identify_learning_opportunities(
        self,
        classification_result: ClassificationResult,
        processing_result: Optional[EnhancedProcessingResult],
        context: Optional[Dict[str, Any]]
    ) -> List[str]:
        """Identify opportunities for pattern learning"""
        opportunities = []
        
        # Low confidence classification
        if classification_result.confidence < 0.7:
            opportunities.append("low_confidence_classification")
        
        # Security flags detected
        if classification_result.security_flags:
            opportunities.append("security_pattern_learning")
        
        # Processing anomalies
        if processing_result and processing_result.anomalies:
            opportunities.append("anomaly_pattern_learning")
        
        # Low context relevance
        if classification_result.context_relevance < 0.5:
            opportunities.append("context_relevance_improvement")
        
        return opportunities
    
    def _update_enhanced_metrics(self, result: EnhancedIngestionResult):
        """Update performance metrics with enhanced result"""
        self._metrics["enhanced_ingestions"] += 1
        
        if result.memory_enhanced:
            self._metrics["memory_enhanced_operations"] += 1
        
        if result.patterns_applied:
            self._metrics["patterns_applied"] += len(result.patterns_applied)
        
        # Update running averages
        count = self._metrics["enhanced_ingestions"]
        
        # Average processing time
        current_avg_time = self._metrics["avg_processing_time"]
        self._metrics["avg_processing_time"] = (
            (current_avg_time * (count - 1) + result.processing_time_ms) / count
        )
        
        # Average classification confidence
        current_avg_conf = self._metrics["avg_classification_confidence"]
        self._metrics["avg_classification_confidence"] = (
            (current_avg_conf * (count - 1) + result.classification_result.confidence) / count
        )
        
        # Average context relevance
        current_avg_relevance = self._metrics["avg_context_relevance"]
        self._metrics["avg_context_relevance"] = (
            (current_avg_relevance * (count - 1) + result.classification_result.context_relevance) / count
        )
    
    async def _record_enhanced_operation(self, result: EnhancedIngestionResult):
        """Record enhanced operation in session service"""
        try:
            await self._session_service.record_data_upload_operation(
                session_id=result.session_id,
                data_id=result.data_id,
                filename=result.file_name,
                file_size=result.file_size,
                metadata={
                    "data_type": result.data_type,
                    "processing_status": result.processing_status,
                    "memory_enhanced": result.memory_enhanced,
                    "classification_confidence": result.classification_result.confidence,
                    "context_relevance": result.classification_result.context_relevance,
                    "patterns_applied": len(result.patterns_applied),
                    "learning_opportunities": len(result.learning_opportunities),
                    "security_flags": result.classification_result.security_flags,
                    "processing_time_ms": result.processing_time_ms
                }
            )
        except Exception as e:
            self.logger.warning(f"Failed to record enhanced operation: {e}")
    
    def _generate_processing_recommendations(
        self,
        insights: Dict[str, Any],
        processing_history: List[Dict[str, Any]]
    ) -> List[str]:
        """Generate recommendations based on processing insights"""
        recommendations = []
        
        # Classification accuracy recommendations
        if insights["classification_accuracy"] < 0.8:
            recommendations.append(
                "Classification accuracy is below optimal. Consider providing feedback "
                "on misclassified data to improve pattern learning."
            )
        
        # Memory enhancement recommendations
        if insights["memory_enhancement_rate"] < 0.5:
            recommendations.append(
                "Memory enhancement rate is low. Ensure conversation context is available "
                "for better processing results."
            )
        
        # Pattern utilization recommendations
        pattern_stats = insights["pattern_utilization"]
        if pattern_stats.get("total_patterns", 0) < 10:
            recommendations.append(
                "Limited learned patterns available. Provide more feedback to improve "
                "automated pattern recognition."
            )
        
        # Data type diversity recommendations
        data_types = insights["data_types"]
        if len(data_types) == 1:
            recommendations.append(
                "Consider uploading diverse data types to improve overall system learning."
            )
        
        # Learning opportunities
        learning_opps = insights["learning_opportunities"]
        if len(learning_opps) > len(processing_history) * 0.3:
            recommendations.append(
                "Multiple learning opportunities detected. Review and provide feedback "
                "on recent processing results to enhance system performance."
            )
        
        return recommendations
    
    def _generate_data_id(self, content: str) -> str:
        """Generate unique ID from content hash"""
        hash_object = hashlib.sha256(content.encode("utf-8"))
        return f"enhanced_data_{hash_object.hexdigest()[:16]}"
    
    async def health_check(self) -> Dict[str, Any]:
        """Enhanced health check including memory and pattern learning components"""
        base_health = await super().health_check()
        
        # Check enhanced components
        components = {
            "memory_service": "unknown",
            "enhanced_classifier": "unknown",
            "enhanced_processor": "unknown",
            "pattern_learner": "unknown"
        }
        
        # Check memory service
        try:
            if self._memory_service:
                # Test memory service with a simple operation
                test_context = await self._memory_service.retrieve_context("health_check", "test")
                components["memory_service"] = "healthy"
            else:
                components["memory_service"] = "unavailable"
        except Exception:
            components["memory_service"] = "unhealthy"
        
        # Check enhanced classifier
        try:
            if self._enhanced_classifier:
                components["enhanced_classifier"] = "healthy"
            else:
                components["enhanced_classifier"] = "unavailable"
        except Exception:
            components["enhanced_classifier"] = "unhealthy"
        
        # Check enhanced processor
        try:
            if self._enhanced_processor:
                components["enhanced_processor"] = "healthy"
            else:
                components["enhanced_processor"] = "unavailable"
        except Exception:
            components["enhanced_processor"] = "unhealthy"
        
        # Check pattern learner
        try:
            if self._pattern_learner:
                pattern_stats = self._pattern_learner.get_pattern_statistics()
                components["pattern_learner"] = "healthy"
            else:
                components["pattern_learner"] = "unavailable"
        except Exception:
            components["pattern_learner"] = "unhealthy"
        
        # Determine overall status
        unhealthy_components = [
            comp for comp, status in components.items()
            if "unhealthy" in str(status)
        ]
        
        if unhealthy_components:
            overall_status = "degraded"
        elif any("unavailable" in str(status) for status in components.values()):
            overall_status = "degraded"
        else:
            overall_status = "healthy"
        
        # Enhanced health information
        enhanced_health = {
            **base_health,
            "service": "enhanced_data_service",
            "status": overall_status,
            "enhanced_components": components,
            "metrics": self._metrics.copy(),
            "capabilities": {
                "memory_enhanced_processing": True,
                "pattern_learning": True,
                "context_aware_classification": True,
                "security_pattern_detection": True,
                "adaptive_processing": True,
                "cross_session_learning": True
            }
        }
        
        return enhanced_health