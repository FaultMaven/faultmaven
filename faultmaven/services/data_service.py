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
from datetime import datetime
from typing import Any, Dict, List, Optional

from faultmaven.services.base_service import BaseService
from faultmaven.models.interfaces import IDataClassifier, ILogProcessor, ISanitizer, ITracer, IStorageBackend
from faultmaven.models import (
    DataInsightsResponse,
    DataType,
    UploadedData,
)
from faultmaven.exceptions import ValidationException, ServiceException


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
        """
        super().__init__()
        self._classifier = data_classifier
        self._processor = log_processor
        self._sanitizer = sanitizer
        self._tracer = tracer
        self._storage = storage_backend
        self._session_service = session_service

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


class SimpleStorageBackend(IStorageBackend):
    """Simple in-memory storage backend for testing and development"""
    
    def __init__(self):
        self._storage: Dict[str, Any] = {}
        self._session_index: Dict[str, List[str]] = {}  # session_id -> list of data_ids
    
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