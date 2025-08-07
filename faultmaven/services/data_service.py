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
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from faultmaven.models.interfaces import IDataClassifier, ILogProcessor, ISanitizer, ITracer, IStorageBackend
from faultmaven.models import (
    DataInsightsResponse,
    DataType,
    UploadedData,
)


class DataService:
    """Service for managing data processing workflows with interface dependencies"""

    def __init__(
        self,
        data_classifier: IDataClassifier,
        log_processor: ILogProcessor,
        sanitizer: ISanitizer,
        tracer: ITracer,
        storage_backend: Optional[IStorageBackend] = None,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize the Data Service with interface dependencies

        Args:
            data_classifier: Data classification service interface
            log_processor: Log processing service interface  
            sanitizer: Data sanitization service interface
            tracer: Distributed tracing interface
            storage_backend: Optional storage backend interface
            logger: Optional logger instance
        """
        self._classifier = data_classifier
        self._processor = log_processor
        self._sanitizer = sanitizer
        self._tracer = tracer
        self._storage = storage_backend
        self._logger = logger or logging.getLogger(__name__)

    async def ingest_data(
        self,
        content: str,
        session_id: str,
        file_name: Optional[str] = None,
        file_size: Optional[int] = None,
    ) -> UploadedData:
        """
        Ingest and process raw data using interface dependencies

        Args:
            content: Raw data content
            session_id: Session identifier
            file_name: Optional original filename
            file_size: Optional file size in bytes

        Returns:
            UploadedData model with processing results

        Raises:
            ValueError: If input validation fails
            RuntimeError: If processing fails
        """
        with self._tracer.trace("data_service_ingest_data"):
            self._logger.info(f"Ingesting data for session {session_id}")

            # Validate input
            if not content:
                raise ValueError("Content cannot be empty")
            
            if not session_id:
                raise ValueError("Session ID cannot be empty")

            try:
                # Generate data ID from content hash
                data_id = self._generate_data_id(content)

                # Sanitize content using interface
                sanitized_content = self._sanitizer.sanitize(content)
                
                # Classify data type using interface
                data_type = await self._classifier.classify(sanitized_content, file_name)
                self._logger.debug(f"Classified data as {data_type.value}")

                # Process data to extract insights using interface
                try:
                    insights_response = await self._processor.process(sanitized_content, data_type)
                    # Convert DataInsightsResponse to insights dict including anomalies
                    detailed_insights = {
                        "error_count": insights_response.insights.get("error_count", 0),
                        "error_rate": insights_response.insights.get("error_rate", 0.0),
                        "processing_time_ms": insights_response.processing_time_ms,
                        "confidence_score": insights_response.confidence_score,
                        "anomalies_detected": insights_response.anomalies_detected,
                        "recommendations": insights_response.recommendations
                    }
                    detailed_insights.update(insights_response.insights)
                except Exception as e:
                    self._logger.warning(f"Failed to extract detailed insights: {e}")
                    detailed_insights = {
                        "processed": True, 
                        "processing_timestamp": datetime.utcnow().isoformat(),
                        "anomalies_detected": [],
                        "recommendations": []
                    }

                # Create uploaded data model
                uploaded_data = UploadedData(
                    data_id=data_id,
                    session_id=session_id,
                    data_type=data_type,
                    content=sanitized_content,
                    file_name=file_name,
                    file_size=file_size or len(content),
                    uploaded_at=datetime.utcnow(),
                    processing_status="completed",
                    insights=detailed_insights
                )

                # Store if backend available
                if self._storage:
                    await self._storage.store(data_id, uploaded_data)
                    self._logger.debug(f"Stored data {data_id} in backend")

                self._logger.info(f"Successfully ingested data {data_id}")
                return uploaded_data

            except Exception as e:
                self._logger.error(f"Failed to ingest data: {e}")
                raise RuntimeError(f"Data ingestion failed: {str(e)}") from e

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
        with self._tracer.trace("data_service_analyze_data"):
            self._logger.debug(f"Analyzing data {data_id} for session {session_id}")

            # Validate inputs
            if not data_id:
                raise ValueError("Data ID cannot be empty")
            if not session_id:
                raise ValueError("Session ID cannot be empty")

            try:
                # Retrieve data from storage
                if not self._storage:
                    raise ValueError("No storage backend available")

                data = await self._storage.retrieve(data_id)
                if not data:
                    raise ValueError(f"Data not found: {data_id}")

                # Verify session ownership
                if data.session_id != session_id:
                    raise ValueError(f"Data {data_id} does not belong to session {session_id}")

                # Process using interface
                start_time = datetime.utcnow()
                insights = await self._processor.process(data.content, data.data_type)
                end_time = datetime.utcnow()
                
                processing_time_ms = int((end_time - start_time).total_seconds() * 1000)

                # Sanitize insights
                sanitized_insights = self._sanitizer.sanitize(insights)
                
                # Detect anomalies based on insights
                anomalies = self._detect_anomalies(data, sanitized_insights)
                
                # Generate recommendations
                recommendations = self._generate_recommendations(data, anomalies)
                
                # Calculate confidence score
                confidence_score = self._calculate_confidence_score(data, sanitized_insights)

                response = DataInsightsResponse(
                    data_id=data_id,
                    data_type=data.data_type,
                    insights=sanitized_insights,
                    confidence_score=confidence_score,
                    processing_time_ms=processing_time_ms,
                    anomalies_detected=anomalies,
                    recommendations=recommendations,
                )

                self._logger.info(f"Successfully analyzed data {data_id}")
                return response

            except ValueError:
                raise
            except Exception as e:
                self._logger.error(f"Failed to analyze data: {e}")
                raise RuntimeError(f"Data analysis failed: {str(e)}") from e

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
        with self._tracer.trace("data_service_batch_process"):
            self._logger.info(f"Batch processing {len(data_items)} items for session {session_id}")

            if not data_items:
                return []

            results = []
            for i, (content, filename) in enumerate(data_items):
                try:
                    result = await self.ingest_data(
                        content=content,
                        session_id=session_id,
                        file_name=filename,
                    )
                    results.append(result)
                    self._logger.debug(f"Successfully processed item {i+1}/{len(data_items)}")
                except Exception as e:
                    self._logger.error(f"Failed to process item {i+1} ({filename}): {e}")
                    # Continue with other items - don't fail entire batch

            self._logger.info(f"Batch processing completed: {len(results)}/{len(data_items)} items processed")
            return results

    async def get_session_data(self, session_id: str) -> List[UploadedData]:
        """
        Get all data associated with a session

        Args:
            session_id: Session identifier

        Returns:
            List of UploadedData for the session
        """
        with self._tracer.trace("data_service_get_session_data"):
            if not session_id:
                raise ValueError("Session ID cannot be empty")

            try:
                # In a real implementation, this would query the storage backend
                # For now, return empty list
                self._logger.debug(f"Retrieved session data for {session_id}")
                return []
            except Exception as e:
                self._logger.error(f"Failed to get session data: {e}")
                raise RuntimeError(f"Failed to retrieve session data: {str(e)}") from e

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
            elif data.data_type == DataType.STACK_TRACE:
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
            self._logger.warning(f"Anomaly detection failed: {e}")

        return anomalies

    def _generate_recommendations(
        self, data: UploadedData, anomalies: List[Dict[str, Any]]
    ) -> List[str]:
        """
        Generate actionable recommendations based on data and anomalies

        Args:
            data: Uploaded data object
            anomalies: Detected anomalies

        Returns:
            List of recommendation strings
        """
        recommendations = []

        # Base recommendations by data type
        if data.data_type == DataType.ERROR_MESSAGE:
            recommendations.extend([
                "Review error logs for patterns and frequency",
                "Check system resources at error timestamp",
                "Verify error handling and logging configuration",
            ])
        elif data.data_type == DataType.LOG_FILE:
            recommendations.extend([
                "Monitor log patterns for trends over time",
                "Consider implementing log rotation if not present",
                "Review logging levels and verbosity settings",
            ])
        elif data.data_type == DataType.STACK_TRACE:
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
            self._logger.warning(f"Confidence calculation failed: {e}")

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
        with self._tracer.trace("data_service_delete_data"):
            self._logger.debug(f"Deleting data {data_id} for session {session_id}")
            
            # Validate inputs
            if not data_id or not data_id.strip():
                raise ValueError("Data ID cannot be empty")
            if not session_id or not session_id.strip():
                raise ValueError("Session ID cannot be empty")
            
            try:
                if not self._storage:
                    raise ValueError("No storage backend available")
                
                # Retrieve data to verify ownership
                data = await self._storage.retrieve(data_id)
                if not data:
                    raise FileNotFoundError(f"Data {data_id} not found")
                
                # Verify session ownership
                if data.session_id != session_id:
                    raise ValueError(f"Data {data_id} does not belong to session {session_id}")
                
                # Delete from storage
                await self._storage.delete(data_id)
                
                self._logger.info(f"Successfully deleted data {data_id}")
                return True
                
            except ValueError:
                raise
            except FileNotFoundError:
                raise
            except Exception as e:
                self._logger.error(f"Failed to delete data: {e}")
                raise RuntimeError(f"Data deletion failed: {str(e)}") from e

    async def health_check(self) -> Dict[str, Any]:
        """
        Check health of data service and all dependencies
        
        Returns:
            Dictionary with health status and component details
        """
        with self._tracer.trace("data_service_health_check"):
            try:
                health_info = {
                    "service": "data_service",
                    "status": "healthy",
                    "timestamp": datetime.utcnow().isoformat(),
                    "components": {
                        "data_classifier": "unknown",
                        "log_processor": "unknown",
                        "sanitizer": "unknown",
                        "tracer": "unknown",
                        "storage_backend": "unknown"
                    }
                }
                
                # Check data classifier
                try:
                    if self._classifier and hasattr(self._classifier, 'classify'):
                        # Test classification
                        test_result = await self._classifier.classify("test log entry", "test.log")
                        health_info["components"]["data_classifier"] = "healthy"
                    else:
                        health_info["components"]["data_classifier"] = "unavailable"
                except Exception:
                    health_info["components"]["data_classifier"] = "unhealthy"
                
                # Check log processor
                try:
                    if self._processor and hasattr(self._processor, 'process'):
                        health_info["components"]["log_processor"] = "healthy"
                    else:
                        health_info["components"]["log_processor"] = "unavailable"
                except Exception:
                    health_info["components"]["log_processor"] = "unhealthy"
                
                # Check sanitizer
                try:
                    if self._sanitizer and hasattr(self._sanitizer, 'sanitize'):
                        # Test sanitization
                        test_result = self._sanitizer.sanitize("test data")
                        health_info["components"]["sanitizer"] = "healthy"
                    else:
                        health_info["components"]["sanitizer"] = "unavailable"
                except Exception:
                    health_info["components"]["sanitizer"] = "unhealthy"
                
                # Check tracer
                try:
                    if self._tracer and hasattr(self._tracer, 'trace'):
                        health_info["components"]["tracer"] = "healthy"
                    else:
                        health_info["components"]["tracer"] = "unavailable"
                except Exception:
                    health_info["components"]["tracer"] = "unhealthy"
                
                # Check storage backend
                try:
                    if self._storage and hasattr(self._storage, 'store') and hasattr(self._storage, 'retrieve'):
                        health_info["components"]["storage_backend"] = "healthy"
                    else:
                        health_info["components"]["storage_backend"] = "unavailable"
                except Exception:
                    health_info["components"]["storage_backend"] = "unhealthy"
                
                # Determine overall status
                unhealthy_components = [
                    comp for status in health_info["components"].values()
                    for comp in [status] if "unhealthy" in str(status)
                ]
                
                if unhealthy_components:
                    health_info["status"] = "degraded"
                elif any("unavailable" in str(status) for status in health_info["components"].values()):
                    health_info["status"] = "degraded"
                
                return health_info
                
            except Exception as e:
                self._logger.error(f"Health check failed: {e}")
                return {
                    "service": "data_service",
                    "status": "unhealthy",
                    "error": str(e),
                    "timestamp": datetime.utcnow().isoformat()
                }


class SimpleStorageBackend(IStorageBackend):
    """Simple in-memory storage backend for testing and development"""
    
    def __init__(self):
        self._storage: Dict[str, Any] = {}
    
    async def store(self, key: str, data: Any) -> None:
        """Store data in memory"""
        self._storage[key] = data
    
    async def retrieve(self, key: str) -> Optional[Any]:
        """Retrieve data from memory"""
        return self._storage.get(key)
    
    async def delete(self, key: str) -> None:
        """Delete data from memory"""
        if key in self._storage:
            del self._storage[key]