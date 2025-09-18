"""Data Service Refactored Module - Phase 3.2

Purpose: Handles data processing workflows using dependency injection through interfaces

This refactored service manages all data-related operations including ingestion,
classification, processing, and insight extraction using interface dependencies
for better testability and modularity.

Core Responsibilities:
- Data ingestion and validation with interface-based dependencies
- Data type classification through IDataClassifier
- Processing orchestration through ILogProcessor  
- Insight extraction with proper sanitization
- Data storage management through IStorageBackend
- Comprehensive error handling and tracing

Key Improvements:
- Interface-based dependency injection
- Better separation of concerns
- Enhanced testability
- Proper error handling
- Consistent sanitization
"""

import hashlib
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

from faultmaven.services.base import BaseService
from faultmaven.models.interfaces import (
    IDataClassifier, ILogProcessor, ISanitizer, ITracer, IStorageBackend, IMemoryService
)
from faultmaven.models import (
    DataInsightsResponse,
    DataType,
    UploadedData,
)
from faultmaven.exceptions import ValidationException, ServiceException

# Import enhanced components (if available)
try:
    from faultmaven.core.processing.classifier import EnhancedDataClassifier, ClassificationResult
    from faultmaven.core.processing.log_analyzer import EnhancedLogProcessor, EnhancedProcessingResult
    from faultmaven.core.processing.pattern_learner import PatternLearner, PatternType, LearningResult
    ENHANCED_COMPONENTS_AVAILABLE = True
except ImportError:
    # Fallback to regular components when enhanced ones are not available
    ENHANCED_COMPONENTS_AVAILABLE = False


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
    insights: Dict[str, Any]
    memory_enhanced: bool = False
    patterns_applied: List[str] = None
    learning_opportunities: List[str] = None
    processing_time_ms: float = 0.0
    classification_result: Optional[Any] = None  # ClassificationResult if available
    processing_result: Optional[Any] = None     # EnhancedProcessingResult if available

    def __post_init__(self):
        if self.patterns_applied is None:
            self.patterns_applied = []
        if self.learning_opportunities is None:
            self.learning_opportunities = []


class DataService(BaseService):
    """Service for managing data processing workflows with interface dependencies"""

    def __init__(
        self,
        data_classifier: IDataClassifier,
        log_processor: ILogProcessor,
        sanitizer: ISanitizer,
        tracer: ITracer,
        storage_backend: Optional[IStorageBackend] = None,
        session_service=None,  # Optional session service for operation tracking
        settings: Optional[Any] = None,
        memory_service: Optional[IMemoryService] = None,  # Enhanced: Memory service for context
        pattern_learner: Optional[Any] = None,  # Enhanced: Pattern learning service
    ):
        """
        Initialize the Data Service with interface dependencies

        Args:
            data_classifier: Data classification service interface
            log_processor: Log processing service interface  
            sanitizer: Data sanitization service interface
            tracer: Distributed tracing interface
            storage_backend: Optional storage backend interface
            session_service: Optional session service for operation tracking
            settings: Configuration settings for the service
            memory_service: Optional memory service for enhanced context-aware processing
            pattern_learner: Optional pattern learning service for adaptive processing
        """
        super().__init__()
        self._classifier = data_classifier
        self._processor = log_processor
        self._sanitizer = sanitizer
        self._tracer = tracer
        self._storage = storage_backend
        self._session_service = session_service
        self._settings = settings
        
        # Enhanced capabilities
        self._memory_service = memory_service
        self._pattern_learner = pattern_learner
        
        # Initialize enhanced components if available
        if ENHANCED_COMPONENTS_AVAILABLE and memory_service:
            try:
                self._enhanced_classifier = EnhancedDataClassifier(memory_service)
                self._enhanced_processor = EnhancedLogProcessor(memory_service)
                if pattern_learner is None:
                    self._pattern_learner = PatternLearner(memory_service)
                self._enhanced_mode = True
            except Exception as e:
                self.logger.warning(f"Enhanced components initialization failed: {e}")
                self._enhanced_mode = False
        else:
            self._enhanced_mode = False
        
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

    def _get_data_attribute(self, data: Any, attribute: str, default=None):
        """Helper method to get data attributes from both dict and object formats"""
        if isinstance(data, dict):
            return data.get(attribute, default)
        else:
            return getattr(data, attribute, default)

    async def ingest_data(
        self,
        content: str,
        session_id: str,
        file_name: Optional[str] = None,
        file_size: Optional[int] = None,
        data_type: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> UploadedData:
        """
        Ingest and process raw data using interface dependencies

        Args:
            content: Raw data content
            session_id: Session identifier
            file_name: Optional original filename
            file_size: Optional file size in bytes
            data_type: Optional data type override
            context: Optional additional context data

        Returns:
            UploadedData model with processing results

        Raises:
            ValueError: If input validation fails
            RuntimeError: If processing fails
        """
        def _validate_ingest_inputs(content: str, session_id: str, file_name: Optional[str], file_size: Optional[int], data_type: Optional[str], context: Optional[Dict[str, Any]]) -> None:
            if content is None or (isinstance(content, str) and not content.strip()):
                raise ValidationException("Content cannot be empty")
            if not session_id or not session_id.strip():
                raise ValidationException("Session ID cannot be empty")
        
        return await self.execute_operation(
            "ingest_data",
            self._execute_data_ingestion,
            content,
            session_id,
            file_name,
            file_size,
            data_type,
            context,
            validate_inputs=lambda c, s, f, fs, dt, ctx: _validate_ingest_inputs(c, s, f, fs, dt, ctx)
        )
    
    async def _execute_data_ingestion(
        self,
        content: str,
        session_id: str,
        file_name: Optional[str],
        file_size: Optional[int],
        data_type: Optional[str],
        context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Execute the core data ingestion logic"""
        # Generate data ID from content hash
        data_id = self._generate_data_id(content)

        # Log business event
        self.log_business_event(
            "data_ingestion_started",
            "info",
            {
                "data_id": data_id,
                "session_id": session_id,
                "file_name": file_name,
                "content_size": len(content)
            }
        )

        # Sanitize content using interface
        sanitized_content = self._sanitizer.sanitize(content)
        
        # Classify data type using interface (unless overridden) with tracing
        with self._tracer.trace("data_classification"):
            if data_type:
                # Convert string to DataType enum if needed
                from faultmaven.models import DataType
                if isinstance(data_type, str):
                    try:
                        classified_data_type = DataType(data_type)
                    except ValueError:
                        # If invalid data_type provided, fall back to classification
                        classified_data_type = await self._classifier.classify(sanitized_content, file_name)
                else:
                    classified_data_type = data_type
            else:
                classified_data_type = await self._classifier.classify(sanitized_content, file_name)
        
        # Log classification metric
        self.log_metric(
            "data_classified",
            1,
            "count",
            {
                "data_type": classified_data_type.value,
                "session_id": session_id
            }
        )

        # Process data to extract insights using interface
        try:
            insights_response = await self._processor.process(sanitized_content, classified_data_type)
            # Convert DataInsightsResponse to insights dict including anomalies
            # Safely handle insights response structure
            if hasattr(insights_response, 'insights') and isinstance(insights_response.insights, dict):
                detailed_insights = {
                    "error_count": insights_response.insights.get("error_count", 0),
                    "error_rate": insights_response.insights.get("error_rate", 0.0),
                    "processing_time_ms": getattr(insights_response, 'processing_time_ms', 0),
                    "confidence_score": getattr(insights_response, 'confidence_score', 0.5),
                    "anomalies_detected": getattr(insights_response, 'anomalies_detected', []),
                    "recommendations": getattr(insights_response, 'recommendations', [])
                }
                detailed_insights.update(insights_response.insights)
            else:
                # Handle case where insights_response is not properly structured
                detailed_insights = {
                    "processed": True, 
                    "processing_timestamp": datetime.utcnow().isoformat() + 'Z',
                    "anomalies_detected": [],
                    "recommendations": [],
                    "confidence_score": 0.5
                }
        except Exception as e:
            self.logger.warning(f"Failed to extract detailed insights: {e}")
            detailed_insights = {
                "processed": True, 
                "processing_timestamp": datetime.utcnow().isoformat() + 'Z',
                "anomalies_detected": [],
                "recommendations": []
            }

        # For backwards compatibility with tests, return a dict instead of UploadedData object
        # TODO: Once tests are migrated to v3.1.0, this should return UploadedData(id=data_id, name=file_name, type=classified_data_type.value)
        uploaded_data = {
            "data_id": data_id,
            "session_id": session_id,
            "data_type": classified_data_type.value,
            "content": sanitized_content,
            "file_name": file_name or "unknown",
            "file_size": file_size or len(content),
            "processing_status": "completed",
            "insights": detailed_insights
        }
        

        # Store if backend available
        if self._storage:
            await self._storage.store(data_id, uploaded_data)
        
        # Log successful ingestion
        self.log_business_event(
            "data_ingestion_completed",
            "info",
            {
                "data_id": data_id,
                "session_id": session_id,
                "data_type": classified_data_type.value,
                "processing_status": "completed"  # Fixed status since new model doesn't have this field
            }
        )

        # Record operation in session if session service is available
        if self._session_service and session_id:
            try:
                await self._session_service.record_data_upload_operation(
                    session_id=session_id,
                    data_id=data_id,
                    filename=file_name or "unknown",
                    file_size=file_size or len(content),
                    metadata={
                        "data_type": classified_data_type.value,
                        "processing_status": "completed",  # Fixed status since new model doesn't have this field
                        "insights_count": len(detailed_insights)
                    }
                )
            except Exception as e:
                self.logger.warning(f"Failed to record data upload operation in session: {e}")

        return uploaded_data

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
        
        # Check if enhanced mode is available
        if not self._enhanced_mode:
            # Fallback to regular ingestion with enhanced result format
            regular_result = await self.ingest_data(
                content, session_id, file_name, file_size, data_type, context
            )
            
            # Convert to enhanced result format
            return EnhancedIngestionResult(
                data_id=regular_result.get("data_id", ""),
                session_id=session_id,
                data_type=regular_result.get("data_type", "unknown"),
                content=regular_result.get("content", ""),
                file_name=regular_result.get("file_name", "unknown"),
                file_size=regular_result.get("file_size", 0),
                processing_status=regular_result.get("processing_status", "completed"),
                insights=regular_result.get("insights", {}),
                memory_enhanced=False,
                patterns_applied=[],
                learning_opportunities=[],
                processing_time_ms=(time.time() - start_time) * 1000
            )
        
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
            sanitized_content = self._sanitizer.sanitize(content)
            
            # Enhanced classification with memory context
            classification_result = await self._enhanced_classifier.classify_with_context(
                content=sanitized_content,
                session_id=session_id,
                filename=file_name,
                context=context
            )
            
            # Apply learned patterns for enhanced classification
            classification_patterns = []
            if self._pattern_learner:
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
            security_patterns = []
            if self._pattern_learner:
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
                    f"classification_patterns_{len(classification_patterns)}",
                    f"security_patterns_{len(security_patterns)}"
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
                    "learning_opportunities": len(enhanced_result.learning_opportunities)
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

    async def analyze_data(
        self,
        data_id: str,
        session_id: str,
    ) -> DataInsightsResponse:
        """
        Analyze data and extract insights using interface dependencies

        Args:
            data_id: Data identifier
            session_id: Session identifier

        Returns:
            DataInsightsResponse with extracted insights

        Raises:
            ValueError: If data not found or invalid parameters
            RuntimeError: If analysis fails
        """
        def _validate_analyze_inputs(data_id: str, session_id: str) -> None:
            if data_id is None or (isinstance(data_id, str) and not data_id.strip()):
                raise ValidationException("Data ID cannot be empty")
            if session_id is None or (isinstance(session_id, str) and not session_id.strip()):
                raise ValidationException("Session ID cannot be empty")
        
        return await self.execute_operation(
            "analyze_data",
            self._execute_data_analysis,
            data_id,
            session_id,
            validate_inputs=lambda di, si: _validate_analyze_inputs(di, si)
        )
    
    async def _execute_data_analysis(
        self,
        data_id: str,
        session_id: str
    ) -> DataInsightsResponse:
        """Execute the core data analysis logic"""
        # Retrieve data from storage
        if not self._storage:
            raise RuntimeError("No storage backend available")

        data = await self._storage.retrieve(data_id)
        if not data:
            raise FileNotFoundError(f"Data not found: {data_id}")

        # Verify session ownership (handle both dictionary and object formats)
        data_session_id = data.get('session_id') if isinstance(data, dict) else getattr(data, 'session_id', None)
        if data_session_id != session_id:
            raise ValidationException(f"Data {data_id} does not belong to session {session_id}")
        
        # Log business event  
        data_type_value = self._get_data_attribute(data, 'data_type')
        if hasattr(data_type_value, 'value'):
            data_type_value = data_type_value.value
        self.log_business_event(
            "data_analysis_started",
            "info",
            {
                "data_id": data_id,
                "session_id": session_id,
                "data_type": data_type_value
            }
        )

        # Process using interface with tracing
        start_time = datetime.utcnow()
        with self._tracer.trace("data_analysis_processing"):
            try:
                data_content = self._get_data_attribute(data, 'content', '')
                data_type = self._get_data_attribute(data, 'data_type', DataType.UNKNOWN)
                if isinstance(data_type, str):
                    data_type = DataType(data_type)
                insights_response = await self._processor.process(data_content, data_type)
            except Exception as e:
                # Wrap external processor exceptions in ServiceException
                self.logger.error(f"Data analysis failed for {data_id}: {e}")
                raise ServiceException(
                    f"Data analysis processing failed: {str(e)}", 
                    details={"operation": "analyze_data", "data_id": data_id, "error": str(e)}
                ) from e
        end_time = datetime.utcnow()
        
        # Handle insights response properly
        if hasattr(insights_response, 'insights'):
            insights = insights_response.insights
        else:
            insights = insights_response if isinstance(insights_response, dict) else {"processed": True}
        
        # Use processing time from processor response, or calculate if not available
        if hasattr(insights_response, 'processing_time_ms'):
            processing_time_ms = insights_response.processing_time_ms
        else:
            processing_time_ms = int((end_time - start_time).total_seconds() * 1000)
        
        # Log processing time metric
        self.log_metric(
            "data_analysis_processing_time",
            processing_time_ms,
            "milliseconds",
            {
                "data_id": data_id,
                "data_type": data_type_value
            }
        )

        # Sanitize insights
        sanitized_insights = self._sanitizer.sanitize(insights)
        
        # Ensure insights is a dictionary
        if not isinstance(sanitized_insights, dict):
            sanitized_insights = {"processed": True, "data": str(sanitized_insights)}
        
        # Use anomalies and recommendations from processor response, or generate if not available
        if hasattr(insights_response, 'anomalies_detected'):
            anomalies = insights_response.anomalies_detected
        else:
            anomalies = self._detect_anomalies(data, sanitized_insights)
        
        if hasattr(insights_response, 'recommendations'):
            recommendations = insights_response.recommendations
        else:
            recommendations = self._generate_recommendations(data, anomalies, data_type)
        
        # Use confidence score from processor response, or calculate if not available
        if hasattr(insights_response, 'confidence_score'):
            confidence_score = insights_response.confidence_score
        else:
            confidence_score = self._calculate_confidence_score(data, sanitized_insights)

        # Create response with proper error handling
        try:
            response = DataInsightsResponse(
                data_id=data_id,
                data_type=data_type,
                insights=sanitized_insights,
                confidence_score=confidence_score,
                processing_time_ms=processing_time_ms,
                anomalies_detected=anomalies,
                recommendations=recommendations,
            )
        except Exception as e:
            # Handle model validation errors
            raise RuntimeError(f"Failed to create DataInsightsResponse: {str(e)}") from e
        
        # Log completion event
        self.log_business_event(
            "data_analysis_completed",
            "info",
            {
                "data_id": data_id,
                "session_id": session_id,
                "confidence_score": confidence_score,
                "anomalies_count": len(anomalies)
            }
        )

        return response

    async def batch_process(
        self, 
        data_items: List[tuple[str, Optional[str]]], 
        session_id: str
    ) -> List[UploadedData]:
        """
        Process multiple data items in batch

        Args:
            data_items: List of (content, filename) tuples
            session_id: Session identifier

        Returns:
            List of processed UploadedData
        """
        return await self.execute_operation(
            "batch_process",
            self._execute_batch_processing,
            data_items,
            session_id
        )
    
    async def _execute_batch_processing(
        self,
        data_items: List[tuple[str, Optional[str]]], 
        session_id: str
    ) -> List[UploadedData]:
        """Execute the core batch processing logic"""
        if not data_items:
            return []
        
        # Log batch start event
        self.log_business_event(
            "batch_processing_started",
            "info",
            {
                "session_id": session_id,
                "batch_size": len(data_items)
            }
        )

        results = []
        for i, (content, filename) in enumerate(data_items):
            try:
                result = await self.ingest_data(
                    content=content,
                    session_id=session_id,
                    file_name=filename,
                )
                results.append(result)
            except Exception as e:
                # Log individual item failure but continue with batch
                self.logger.error(f"Failed to process item {i+1} ({filename}): {e}")
                # Continue with other items - don't fail entire batch
        
        # Log batch completion metrics
        self.log_metric(
            "batch_processing_success_rate",
            len(results) / len(data_items) * 100,
            "percent",
            {"session_id": session_id}
        )
        
        self.log_business_event(
            "batch_processing_completed",
            "info",
            {
                "session_id": session_id,
                "successful_items": len(results),
                "total_items": len(data_items),
                "success_rate": len(results) / len(data_items)
            }
        )

        return results

    async def get_session_data(self, session_id: str) -> List[UploadedData]:
        """
        Get all data associated with a session

        Args:
            session_id: Session identifier

        Returns:
            List of UploadedData for the session
        """
        def _validate_session_inputs(session_id: str) -> None:
            if not session_id or not session_id.strip():
                raise ValidationException("Session ID cannot be empty")
        
        return await self.execute_operation(
            "get_session_data",
            self._execute_session_data_retrieval,
            session_id,
            validate_inputs=lambda sid: _validate_session_inputs(sid)
        )
    
    async def _execute_session_data_retrieval(self, session_id: str) -> List[UploadedData]:
        """Execute the core session data retrieval logic"""
        if not self._storage:
            return []
        
        # Check if storage backend supports session-based retrieval
        if hasattr(self._storage, 'retrieve_by_session'):
            session_data = await self._storage.retrieve_by_session(session_id)
            # Filter to ensure all items have data_id (dictionary or object format)
            filtered_data = []
            for data in session_data:
                if isinstance(data, dict) and 'data_id' in data:
                    filtered_data.append(data)
                elif hasattr(data, 'data_id'):
                    filtered_data.append(data)
            return filtered_data
        else:
            # Fallback: scan all storage items for matching session_id
            # This is less efficient but ensures compatibility
            session_data = []
            if hasattr(self._storage, '_storage'):
                for data in self._storage._storage.values():
                    # Handle both dictionary and object formats
                    if isinstance(data, dict):
                        if data.get('session_id') == session_id:
                            session_data.append(data)
                    elif hasattr(data, 'session_id') and data.session_id == session_id:
                        session_data.append(data)
            return session_data

    def _generate_data_id(self, content: str) -> str:
        """Generate unique ID from content hash"""
        hash_object = hashlib.sha256(content.encode("utf-8"))
        return f"data_{hash_object.hexdigest()[:16]}"

    def _detect_anomalies(self, data: UploadedData, insights: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Detect anomalies in the processed data

        Args:
            data: Uploaded data object
            insights: Processing insights

        Returns:
            List of detected anomalies
        """
        anomalies = []

        try:
            # Log-specific anomaly detection
            if data.data_type == DataType.LOG_FILE:
                error_count = insights.get("error_count", 0)
                if isinstance(error_count, (int, float)) and error_count > 100:
                    anomalies.append({
                        "type": "error_spike",
                        "severity": "high",
                        "description": f"High error count detected: {error_count} errors",
                        "value": error_count,
                        "threshold": 100,
                    })

                warning_count = insights.get("warning_count", 0)
                if isinstance(warning_count, (int, float)) and warning_count > 500:
                    anomalies.append({
                        "type": "warning_spike", 
                        "severity": "medium",
                        "description": f"High warning count detected: {warning_count} warnings",
                        "value": warning_count,
                        "threshold": 500,
                    })

            # Stack trace anomaly detection
            elif data_type == DataType.STACK_TRACE:
                frames = insights.get("stack_frames", [])
                if isinstance(frames, list) and len(frames) > 50:
                    anomalies.append({
                        "type": "deep_stack",
                        "severity": "medium",
                        "description": f"Deep stack trace detected: {len(frames)} frames",
                        "value": len(frames),
                        "threshold": 50,
                    })

        except Exception as e:
            self.logger.warning(f"Anomaly detection failed: {e}")

        return anomalies

    def _generate_recommendations(
        self, data: Any, anomalies: List[Dict[str, Any]], data_type: DataType = None
    ) -> List[str]:
        """
        Generate actionable recommendations based on data and anomalies

        Args:
            data: Uploaded data object (dict or UploadedData)
            anomalies: Detected anomalies
            data_type: Data type (optional, will be extracted from data if not provided)

        Returns:
            List of recommendation strings
        """
        recommendations = []

        # Get data type from parameter or data object
        if data_type is None:
            data_type = self._get_data_attribute(data, 'data_type', DataType.UNKNOWN)
            if isinstance(data_type, str):
                data_type = DataType(data_type)

        # Base recommendations by data type
        if data_type == DataType.ERROR_MESSAGE:
            recommendations.extend([
                "Review error logs for patterns and frequency",
                "Check system resources at error timestamp",
                "Verify error handling and logging configuration",
            ])
        elif data_type == DataType.LOG_FILE:
            recommendations.extend([
                "Monitor log patterns for trends over time",
                "Consider implementing log rotation if not present",
                "Review logging levels and verbosity settings",
            ])
        elif data_type == DataType.STACK_TRACE:
            recommendations.extend([
                "Analyze stack trace for root cause identification",
                "Check for memory or resource exhaustion",
                "Review code at the top of the stack trace",
            ])

        # Anomaly-specific recommendations
        for anomaly in anomalies:
            if anomaly.get("type") == "error_spike":
                recommendations.extend([
                    "Investigate root cause of error spike immediately",
                    "Consider implementing circuit breakers or rate limiting",
                    "Check for recent deployments or configuration changes",
                ])
            elif anomaly.get("type") == "warning_spike":
                recommendations.extend([
                    "Review warning patterns to prevent escalation to errors",
                    "Consider adjusting warning thresholds if appropriate",
                ])
            elif anomaly.get("type") == "deep_stack":
                recommendations.extend([
                    "Investigate potential infinite recursion or deep call chains",
                    "Review stack size configuration and limits",
                ])

        return list(set(recommendations))  # Remove duplicates

    def _calculate_confidence_score(
        self, data: UploadedData, insights: Dict[str, Any]
    ) -> float:
        """
        Calculate confidence score for the analysis

        Args:
            data: Uploaded data object
            insights: Processing insights

        Returns:
            Confidence score between 0.0 and 1.0
        """
        base_score = 0.5

        try:
            # Increase score based on data quality
            if insights and not insights.get("processing_error"):
                base_score += 0.2

            # Increase based on data type specificity
            if data.data_type != DataType.UNKNOWN:
                base_score += 0.15

            # Increase if we have specific insights
            insight_indicators = [
                "error_count", "warning_count", "stack_frames", 
                "metrics", "patterns_found"
            ]
            found_insights = sum(1 for indicator in insight_indicators if insights.get(indicator))
            base_score += min(found_insights * 0.05, 0.15)

            # Bonus for structured data
            if data.file_name:
                base_score += 0.05

        except Exception as e:
            self.logger.warning(f"Confidence calculation failed: {e}")

        return min(base_score, 1.0)

    async def delete_data(self, data_id: str, session_id: str) -> bool:
        """
        Delete data with proper validation
        
        Args:
            data_id: Data identifier to delete
            session_id: Session identifier for access control
            
        Returns:
            True if deletion was successful
            
        Raises:
            ValueError: If data_id or session_id is invalid
            FileNotFoundError: If data not found
            RuntimeError: If deletion fails
        """
        def _validate_delete_inputs(data_id: str, session_id: str) -> None:
            if not data_id or not data_id.strip():
                raise ValidationException("Data ID cannot be empty")
            if not session_id or not session_id.strip():
                raise ValidationException("Session ID cannot be empty")
        
        return await self.execute_operation(
            "delete_data",
            self._execute_data_deletion,
            data_id,
            session_id,
            validate_inputs=lambda di, si: _validate_delete_inputs(di, si)
        )
    
    async def _execute_data_deletion(self, data_id: str, session_id: str) -> bool:
        """Execute the core data deletion logic"""
        if not self._storage:
            raise RuntimeError("No storage backend available")
        
        # Retrieve data to verify ownership
        data = await self._storage.retrieve(data_id)
        if not data:
            raise FileNotFoundError(f"Data {data_id} not found")
        
        # Verify session ownership (handle both dictionary and object formats)
        data_session_id = data.get('session_id') if isinstance(data, dict) else getattr(data, 'session_id', None)
        if data_session_id != session_id:
            raise ValidationException(f"Data {data_id} does not belong to session {session_id}")
        
        # Delete from storage
        await self._storage.delete(data_id)
        
        # Log business event
        data_type_value = self._get_data_attribute(data, 'data_type')
        if hasattr(data_type_value, 'value'):
            data_type_value = data_type_value.value
        self.log_business_event(
            "data_deleted",
            "info",
            {
                "data_id": data_id,
                "session_id": session_id,
                "data_type": data_type_value
            }
        )
        
        return True

    async def health_check(self) -> Dict[str, Any]:
        """
        Check health of data service and all dependencies
        
        Returns:
            Dictionary with health status and component details
        """
        # Get base health from BaseService
        base_health = await super().health_check()
        
        # Add component-specific health checks
        components = {
            "data_classifier": "unknown",
            "log_processor": "unknown",
            "sanitizer": "unknown",
            "tracer": "unknown",
            "storage_backend": "unknown"
        }
        
        # Check data classifier
        try:
            if self._classifier and hasattr(self._classifier, 'classify'):
                # Test classification
                test_result = await self._classifier.classify("test log entry", "test.log")
                components["data_classifier"] = "healthy"
            else:
                components["data_classifier"] = "unavailable"
        except Exception:
            components["data_classifier"] = "unhealthy"
        
        # Check log processor
        try:
            if self._processor and hasattr(self._processor, 'process'):
                components["log_processor"] = "healthy"
            else:
                components["log_processor"] = "unavailable"
        except Exception:
            components["log_processor"] = "unhealthy"
        
        # Check sanitizer
        try:
            if self._sanitizer and hasattr(self._sanitizer, 'sanitize'):
                # Test sanitization
                test_result = self._sanitizer.sanitize("test data")
                components["sanitizer"] = "healthy"
            else:
                components["sanitizer"] = "unavailable"
        except Exception:
            components["sanitizer"] = "unhealthy"
        
        # Check tracer
        try:
            if self._tracer and hasattr(self._tracer, 'trace'):
                components["tracer"] = "healthy"
            else:
                components["tracer"] = "unavailable"
        except Exception:
            components["tracer"] = "unhealthy"
        
        # Check storage backend
        try:
            if self._storage and hasattr(self._storage, 'store') and hasattr(self._storage, 'retrieve'):
                components["storage_backend"] = "healthy"
            else:
                components["storage_backend"] = "unavailable"
        except Exception:
            components["storage_backend"] = "unhealthy"
        
        # Determine overall status
        unhealthy_components = [
            comp for status in components.values()
            for comp in [status] if "unhealthy" in str(status)
        ]
        
        status = "healthy"
        if unhealthy_components:
            status = "degraded"
        elif any("unavailable" in str(status) for status in components.values()):
            status = "degraded"
        
        # Combine with base health
        health_info = {
            **base_health,
            "service": "data_service",
            "status": status,
            "components": components
        }
        
        return health_info

    # Enhanced methods for pattern learning and feedback

    async def learn_from_feedback(
        self,
        data_id: str,
        session_id: str,
        user_feedback: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> Optional[Any]:  # LearningResult if available
        """
        Learn from user feedback to improve future processing
        
        Args:
            data_id: Identifier of the data that was processed
            session_id: Session identifier
            user_feedback: User feedback and corrections
            context: Optional additional context
            
        Returns:
            LearningResult with learning outcome details (if enhanced mode available)
        """
        if not self._enhanced_mode or not self._pattern_learner:
            self.logger.warning("Learning from feedback requested but enhanced mode not available")
            return None
            
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
                    "patterns_learned": getattr(learning_result, 'patterns_learned', 0),
                    "patterns_updated": getattr(learning_result, 'patterns_updated', 0),
                    "learning_confidence": getattr(learning_result, 'learning_confidence', 0.0)
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
                
                if hasattr(result, 'classification_result') and result.classification_result:
                    total_confidence += getattr(result.classification_result, 'confidence', 0.5)
                else:
                    total_confidence += 0.5
                
                if result.memory_enhanced:
                    memory_enhanced_count += 1
                
                # Collect learning opportunities
                if hasattr(result, 'learning_opportunities') and result.learning_opportunities:
                    insights["learning_opportunities"].extend(result.learning_opportunities)
            
            insights["data_types"] = type_counts
            insights["classification_accuracy"] = total_confidence / len(filtered_history)
            insights["memory_enhancement_rate"] = memory_enhanced_count / len(filtered_history)
            
            # Get pattern statistics if pattern learner is available
            if self._pattern_learner and hasattr(self._pattern_learner, 'get_pattern_statistics'):
                try:
                    pattern_stats = self._pattern_learner.get_pattern_statistics()
                    insights["pattern_utilization"] = pattern_stats
                except Exception as e:
                    self.logger.warning(f"Failed to get pattern statistics: {e}")
                    insights["pattern_utilization"] = {}
            
            # Generate recommendations
            insights["recommendations"] = self._generate_processing_recommendations(
                insights, filtered_history
            )
            
            return insights
            
        except Exception as e:
            self.logger.error(f"Failed to get processing insights: {e}")
            return {"error": str(e)}

    # Enhanced helper methods

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
        classification_result: Any,  # ClassificationResult if available
        processing_result: Optional[Any],  # EnhancedProcessingResult if available
        classification_patterns: List[Dict[str, Any]],
        security_patterns: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Combine insights from all processing components"""
        combined = {
            "classification": {
                "data_type": getattr(classification_result, 'data_type', DataType.UNKNOWN).value,
                "confidence": getattr(classification_result, 'confidence', 0.5),
                "context_relevance": getattr(classification_result, 'context_relevance', 0.0),
                "pattern_matches": getattr(classification_result, 'pattern_matches', []),
                "security_flags": getattr(classification_result, 'security_flags', []),
                "memory_enhanced": getattr(classification_result, 'memory_enhanced', False)
            },
            "patterns": {
                "classification_patterns": classification_patterns,
                "security_patterns": security_patterns
            }
        }
        
        if processing_result:
            combined["processing"] = {
                "insights": getattr(processing_result, 'insights', {}),
                "anomalies": getattr(processing_result, 'anomalies', []),
                "recommendations": getattr(processing_result, 'recommendations', []),
                "confidence_score": getattr(processing_result, 'confidence_score', 0.5),
                "context_relevance": getattr(processing_result, 'context_relevance', 0.0),
                "security_flags": getattr(processing_result, 'security_flags', []),
                "memory_enhanced": getattr(processing_result, 'memory_enhanced', False)
            }
        
        return combined

    def _identify_learning_opportunities(
        self,
        classification_result: Any,
        processing_result: Optional[Any],
        context: Optional[Dict[str, Any]]
    ) -> List[str]:
        """Identify opportunities for pattern learning"""
        opportunities = []
        
        # Low confidence classification
        if hasattr(classification_result, 'confidence') and classification_result.confidence < 0.7:
            opportunities.append("low_confidence_classification")
        
        # Security flags detected
        if hasattr(classification_result, 'security_flags') and classification_result.security_flags:
            opportunities.append("security_pattern_learning")
        
        # Processing anomalies
        if processing_result and hasattr(processing_result, 'anomalies') and processing_result.anomalies:
            opportunities.append("anomaly_pattern_learning")
        
        # Low context relevance
        if hasattr(classification_result, 'context_relevance') and classification_result.context_relevance < 0.5:
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
        if result.classification_result and hasattr(result.classification_result, 'confidence'):
            current_avg_conf = self._metrics["avg_classification_confidence"]
            self._metrics["avg_classification_confidence"] = (
                (current_avg_conf * (count - 1) + result.classification_result.confidence) / count
            )
        
        # Average context relevance
        if result.classification_result and hasattr(result.classification_result, 'context_relevance'):
            current_avg_relevance = self._metrics["avg_context_relevance"]
            self._metrics["avg_context_relevance"] = (
                (current_avg_relevance * (count - 1) + result.classification_result.context_relevance) / count
            )

    async def _record_enhanced_operation(self, result: EnhancedIngestionResult):
        """Record enhanced operation in session service"""
        try:
            metadata = {
                "data_type": result.data_type,
                "processing_status": result.processing_status,
                "memory_enhanced": result.memory_enhanced,
                "patterns_applied": len(result.patterns_applied),
                "learning_opportunities": len(result.learning_opportunities),
                "processing_time_ms": result.processing_time_ms
            }
            
            # Add classification metrics if available
            if result.classification_result:
                metadata.update({
                    "classification_confidence": getattr(result.classification_result, 'confidence', 0.5),
                    "context_relevance": getattr(result.classification_result, 'context_relevance', 0.0),
                    "security_flags": getattr(result.classification_result, 'security_flags', [])
                })
            
            await self._session_service.record_data_upload_operation(
                session_id=result.session_id,
                data_id=result.data_id,
                filename=result.file_name,
                file_size=result.file_size,
                metadata=metadata
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
        if isinstance(pattern_stats, dict) and pattern_stats.get("total_patterns", 0) < 10:
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


class SimpleStorageBackend(IStorageBackend):
    """Simple in-memory storage backend for testing and development"""
    
    def __init__(self, settings: Optional[Any] = None):
        self._storage: Dict[str, Any] = {}
        self._session_index: Dict[str, List[str]] = {}  # session_id -> list of data_ids
        self._settings = settings
    
    async def store(self, key: str, data: Any) -> None:
        """Store data in memory and maintain session index"""
        self._storage[key] = data
        
        # Update session index if data has session_id (handle both dict and object formats)
        session_id = None
        if isinstance(data, dict):
            session_id = data.get('session_id')
        elif hasattr(data, 'session_id'):
            session_id = data.session_id
        
        if session_id:
            if session_id not in self._session_index:
                self._session_index[session_id] = []
            if key not in self._session_index[session_id]:
                self._session_index[session_id].append(key)
    
    async def retrieve(self, key: str) -> Optional[Any]:
        """Retrieve data from memory"""
        return self._storage.get(key)
    
    async def retrieve_by_session(self, session_id: str) -> List[Any]:
        """Retrieve all data for a given session"""
        if session_id not in self._session_index:
            return []
        
        session_data = []
        for data_id in self._session_index[session_id]:
            data = self._storage.get(data_id)
            if data is not None:
                session_data.append(data)
        
        return session_data
    
    async def delete(self, key: str) -> None:
        """Delete data from memory and update session index"""
        if key in self._storage:
            data = self._storage[key]
            del self._storage[key]
            
            # Update session index (handle both dict and object formats)
            session_id = None
            if isinstance(data, dict):
                session_id = data.get('session_id')
            elif hasattr(data, 'session_id'):
                session_id = data.session_id
            
            if session_id and session_id in self._session_index and key in self._session_index[session_id]:
                self._session_index[session_id].remove(key)