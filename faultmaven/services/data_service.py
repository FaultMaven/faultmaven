"""Data Service Module

Purpose: Handles data processing workflows and transformations

This service manages all data-related operations including ingestion,
classification, processing, and insight extraction from various data types.

Core Responsibilities:
- Data ingestion and validation
- Data type classification
- Processing orchestration
- Insight extraction
- Data storage management
"""

import hashlib
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from faultmaven.core.processing.classifier import DataClassifier
from faultmaven.core.processing.log_analyzer import LogProcessor
from faultmaven.models import DataInsightsResponse, DataType, UploadedData
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.infrastructure.security.redaction import DataSanitizer


class DataService:
    """Service for managing data processing workflows"""

    def __init__(
        self,
        data_classifier: DataClassifier,
        log_processor: LogProcessor,
        data_sanitizer: DataSanitizer,
        storage_backend: Optional[Any] = None,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize the Data Service

        Args:
            data_classifier: Data classification service
            log_processor: Log processing service
            data_sanitizer: Data sanitization service
            storage_backend: Optional storage backend for persisting data
            logger: Optional logger instance
        """
        self.data_classifier = data_classifier
        self.log_processor = log_processor
        self.data_sanitizer = data_sanitizer
        self.storage_backend = storage_backend
        self.logger = logger or logging.getLogger(__name__)

    @trace("data_service_ingest_data")
    async def ingest_data(
        self,
        content: str,
        session_id: str,
        file_name: Optional[str] = None,
        file_size: Optional[int] = None,
    ) -> UploadedData:
        """
        Ingest and process raw data

        Args:
            content: Raw data content
            session_id: Session identifier
            file_name: Optional original filename
            file_size: Optional file size in bytes

        Returns:
            UploadedData model with processing results

        Raises:
            ValueError: If data validation fails
            RuntimeError: If processing fails
        """
        self.logger.info(f"Ingesting data for session {session_id}")

        # Validate input
        if not content:
            raise ValueError("Content cannot be empty")

        # Generate data ID from content hash
        data_id = self._generate_data_id(content)

        try:
            # Sanitize content
            sanitized_content = self.data_sanitizer.sanitize(content)

            # Classify data type
            data_type = await self._classify_data(sanitized_content, file_name)
            self.logger.debug(f"Classified data as {data_type.value}")

            # Create uploaded data model
            uploaded_data = UploadedData(
                data_id=data_id,
                session_id=session_id,
                data_type=data_type,
                content=sanitized_content,
                file_name=file_name,
                file_size=file_size or len(content.encode("utf-8")),
                uploaded_at=datetime.utcnow(),
                processing_status="processing",
            )

            # Process data based on type
            insights = await self._process_by_type(uploaded_data)
            uploaded_data.insights = insights
            uploaded_data.processing_status = "completed"

            # Store if backend available
            if self.storage_backend:
                await self._store_data(uploaded_data)

            self.logger.info(f"Successfully ingested data {data_id}")
            return uploaded_data

        except Exception as e:
            self.logger.error(f"Failed to ingest data: {e}")
            raise RuntimeError(f"Data ingestion failed: {str(e)}") from e

    @trace("data_service_extract_insights")
    async def extract_insights(
        self, data_id: str, session_id: str
    ) -> DataInsightsResponse:
        """
        Extract insights from processed data

        Args:
            data_id: Data identifier
            session_id: Session identifier

        Returns:
            DataInsightsResponse with extracted insights
        """
        self.logger.debug(f"Extracting insights for data {data_id}")

        try:
            # Retrieve data (mock for now)
            uploaded_data = await self._retrieve_data(data_id, session_id)
            if not uploaded_data:
                raise ValueError(f"Data {data_id} not found")

            # Process insights based on data type
            start_time = datetime.utcnow()
            
            insights = uploaded_data.insights or {}
            anomalies = await self._detect_anomalies(uploaded_data)
            recommendations = await self._generate_recommendations(
                uploaded_data, anomalies
            )
            
            end_time = datetime.utcnow()
            processing_time_ms = int((end_time - start_time).total_seconds() * 1000)

            # Calculate confidence score based on data quality
            confidence_score = self._calculate_confidence_score(uploaded_data, insights)

            return DataInsightsResponse(
                data_id=data_id,
                data_type=uploaded_data.data_type,
                insights=insights,
                confidence_score=confidence_score,
                processing_time_ms=processing_time_ms,
                anomalies_detected=anomalies,
                recommendations=recommendations,
            )

        except Exception as e:
            self.logger.error(f"Failed to extract insights: {e}")
            raise

    @trace("data_service_batch_process")
    async def batch_process(
        self, data_items: List[Tuple[str, str]], session_id: str
    ) -> List[UploadedData]:
        """
        Process multiple data items in batch

        Args:
            data_items: List of (content, filename) tuples
            session_id: Session identifier

        Returns:
            List of processed UploadedData
        """
        self.logger.info(f"Batch processing {len(data_items)} items")
        
        results = []
        for content, filename in data_items:
            try:
                result = await self.ingest_data(
                    content=content,
                    session_id=session_id,
                    file_name=filename,
                )
                results.append(result)
            except Exception as e:
                self.logger.error(f"Failed to process {filename}: {e}")
                # Continue with other items
                
        return results

    @trace("data_service_get_session_data")
    async def get_session_data(self, session_id: str) -> List[UploadedData]:
        """
        Get all data associated with a session

        Args:
            session_id: Session identifier

        Returns:
            List of UploadedData for the session
        """
        try:
            # In real implementation, query from storage
            return []
        except Exception as e:
            self.logger.error(f"Failed to get session data: {e}")
            raise

    def _generate_data_id(self, content: str) -> str:
        """Generate unique ID from content hash"""
        hash_object = hashlib.sha256(content.encode("utf-8"))
        return f"data_{hash_object.hexdigest()[:12]}"

    async def _classify_data(
        self, content: str, file_name: Optional[str] = None
    ) -> DataType:
        """Classify data type using the classifier"""
        try:
            # Use classifier if available
            if hasattr(self, "data_classifier"):
                return await self.data_classifier.classify(content, file_name)
            
            # Fallback classification based on content patterns
            content_lower = content.lower()
            
            if any(keyword in content_lower for keyword in ["error", "exception", "traceback"]):
                if "traceback" in content_lower or "stack trace" in content_lower:
                    return DataType.STACK_TRACE
                return DataType.ERROR_MESSAGE
            elif any(keyword in content_lower for keyword in ["[", "timestamp", "log"]):
                return DataType.LOG_FILE
            elif any(keyword in content_lower for keyword in ["metric", "gauge", "counter"]):
                return DataType.METRICS_DATA
            elif file_name and any(ext in file_name.lower() for ext in [".conf", ".yaml", ".yml", ".json"]):
                return DataType.CONFIG_FILE
            else:
                return DataType.UNKNOWN
                
        except Exception as e:
            self.logger.warning(f"Classification failed, defaulting to UNKNOWN: {e}")
            return DataType.UNKNOWN

    async def _process_by_type(self, data: UploadedData) -> Dict[str, Any]:
        """Process data based on its type"""
        insights = {
            "data_type": data.data_type.value,
            "processing_timestamp": datetime.utcnow().isoformat(),
        }

        try:
            if data.data_type in [DataType.LOG_FILE, DataType.ERROR_MESSAGE]:
                # Process logs
                if hasattr(self, "log_processor"):
                    log_insights = await self.log_processor.process(data.content)
                    insights.update(log_insights)
                else:
                    insights["message"] = "Log processing not available"
                    
            elif data.data_type == DataType.STACK_TRACE:
                # Extract stack trace details
                insights["stack_frames"] = self._parse_stack_trace(data.content)
                
            elif data.data_type == DataType.METRICS_DATA:
                # Parse metrics
                insights["metrics"] = self._parse_metrics(data.content)
                
            elif data.data_type == DataType.CONFIG_FILE:
                # Validate configuration
                insights["config_valid"] = True
                insights["config_warnings"] = []
                
            else:
                insights["message"] = "Data type not yet supported for processing"

        except Exception as e:
            self.logger.error(f"Processing failed for {data.data_type}: {e}")
            insights["processing_error"] = str(e)

        return insights

    async def _detect_anomalies(self, data: UploadedData) -> List[Dict[str, Any]]:
        """Detect anomalies in the data"""
        anomalies = []
        
        if data.data_type == DataType.LOG_FILE and data.insights:
            # Check for error spikes
            error_count = data.insights.get("error_count", 0)
            if error_count > 100:
                anomalies.append({
                    "type": "error_spike",
                    "severity": "high",
                    "description": f"High error count detected: {error_count} errors",
                    "value": error_count,
                })
                
        return anomalies

    async def _generate_recommendations(
        self, data: UploadedData, anomalies: List[Dict[str, Any]]
    ) -> List[str]:
        """Generate recommendations based on data and anomalies"""
        recommendations = []
        
        # Base recommendations on data type
        if data.data_type == DataType.ERROR_MESSAGE:
            recommendations.append("Review error logs for patterns")
            recommendations.append("Check system resources at error timestamp")
            
        # Add anomaly-based recommendations
        for anomaly in anomalies:
            if anomaly["type"] == "error_spike":
                recommendations.append("Investigate root cause of error spike")
                recommendations.append("Consider implementing rate limiting")
                
        return recommendations

    def _calculate_confidence_score(
        self, data: UploadedData, insights: Dict[str, Any]
    ) -> float:
        """Calculate confidence score for the insights"""
        score = 0.5  # Base score
        
        # Increase based on data quality
        if data.insights and not insights.get("processing_error"):
            score += 0.2
            
        # Increase based on data type specificity
        if data.data_type != DataType.UNKNOWN:
            score += 0.2
            
        # Increase if insights contain specific findings
        if insights.get("error_count", 0) > 0 or insights.get("stack_frames"):
            score += 0.1
            
        return min(score, 1.0)

    def _parse_stack_trace(self, content: str) -> List[Dict[str, Any]]:
        """Parse stack trace content"""
        # Simple parsing logic - in production would be more sophisticated
        frames = []
        lines = content.split("\n")
        
        for i, line in enumerate(lines):
            if "File" in line and "line" in line:
                frames.append({
                    "frame_number": len(frames),
                    "file": line.strip(),
                    "line_number": i + 1,
                })
                
        return frames

    def _parse_metrics(self, content: str) -> Dict[str, Any]:
        """Parse metrics data"""
        # Simple parsing - real implementation would handle various formats
        return {
            "metric_count": content.count("\n"),
            "format": "unknown",
        }

    async def _store_data(self, data: UploadedData) -> None:
        """Store data in backend if available"""
        if self.storage_backend:
            # Implement storage logic
            pass

    async def _retrieve_data(
        self, data_id: str, session_id: str
    ) -> Optional[UploadedData]:
        """Retrieve data from storage"""
        # Mock implementation - real would query storage
        return None