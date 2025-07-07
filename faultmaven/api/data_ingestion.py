"""data_ingestion.py

Purpose: /data endpoint implementation

Requirements:
--------------------------------------------------------------------------------
• Handle multipart/form-data requests
• Route content to appropriate processor
• Return DataInsightsResponse

Key Components:
--------------------------------------------------------------------------------
  router = APIRouter()
  @router.post('/data')

Technology Stack:
--------------------------------------------------------------------------------
FastAPI, Pydantic

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Cost-Efficiency: Use semantic caching
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import logging
import os
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from ..data_processing.classifier import DataClassifier
from ..data_processing.log_processor import LogProcessor
from ..models import DataInsightsResponse, DataType, UploadedData
from ..observability.tracing import trace
from ..session_management import SessionManager

router = APIRouter(prefix="/data", tags=["data_ingestion"])

# Global instances (in production, these would be dependency injected)

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
session_manager = SessionManager(redis_url=redis_url)
data_classifier = DataClassifier()
log_processor = LogProcessor()


def get_session_manager():
    return session_manager


def get_data_classifier():
    return data_classifier


def get_log_processor():
    return log_processor


@router.post("/")
@trace("api_upload_data")
async def upload_data(
    file: UploadFile = File(...),
    session_id: str = Form(...),
    description: Optional[str] = Form(None),
    session_manager: SessionManager = Depends(get_session_manager),
    data_classifier: DataClassifier = Depends(get_data_classifier),
    log_processor: LogProcessor = Depends(get_log_processor),
) -> DataInsightsResponse:
    """
    Upload and process data for troubleshooting analysis

    Args:
        file: File to upload
        session_id: Session identifier
        description: Optional description of the data

    Returns:
        DataInsightsResponse with processing results
    """
    logger = logging.getLogger(__name__)
    logger.info(f"Processing data upload for session {session_id}")

    try:
        # Validate session
        session = await session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Generate data ID
        data_id = str(uuid.uuid4())

        # Read file content
        content = await file.read()
        content_str = content.decode("utf-8", errors="ignore")

        # Classify the data
        logger.info(f"Classifying data {data_id}")
        data_type = await data_classifier.classify(content_str)

        # Create uploaded data record (include data_type)
        uploaded_data = UploadedData(
            data_id=data_id,
            session_id=session_id,
            content=content_str,
            file_name=file.filename,
            file_size=len(content),
            uploaded_at=datetime.utcnow(),
            data_type=data_type,
            insights=None,  # Will be populated after processing
        )

        # Process based on data type
        if data_type == DataType.LOG_FILE:
            logger.info(f"Processing log file {data_id}")
            # Create basic agent state for context-aware processing
            from ..models import AgentState

            agent_state: AgentState = {
                "session_id": session_id,
                "user_query": description or "Data upload for analysis",
                "current_phase": "data_processing",
                "investigation_context": {
                    "data_id": data_id,
                    "data_type": data_type.value,
                },
                "findings": [],
                "recommendations": [],
                "confidence_score": 0.0,
                "tools_used": ["log_processor"],
            }
            result = await log_processor.process(content_str, data_id, agent_state)
        else:
            # For other data types, create basic insights
            result = DataInsightsResponse(
                data_id=data_id,
                data_type=data_type,
                insights={
                    "data_type": data_type.value,
                    "file_name": file.filename,
                    "file_size": len(content),
                    "description": description or "No description provided",
                    "classification_confidence": data_classifier.get_classification_confidence(
                        content_str, data_type
                    ),
                },
                confidence_score=data_classifier.get_classification_confidence(
                    content_str, data_type
                ),
                processing_time_ms=0,
                anomalies_detected=[],
                recommendations=[
                    f"Data classified as {data_type.value}",
                    "Consider uploading additional context if needed",
                ],
            )

        # Update uploaded data with insights
        uploaded_data.insights = result.insights
        uploaded_data.processing_status = "completed"

        # Add to session
        await session_manager.add_data_upload(session_id, data_id)

        # Add investigation history
        await session_manager.add_investigation_history(
            session_id,
            {
                "action": "data_upload",
                "data_id": data_id,
                "data_type": data_type.value,
                "file_name": file.filename,
                "insights": result.insights,
            },
        )

        logger.info(f"Successfully processed data {data_id}")
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Data processing failed: {e}")
        raise HTTPException(status_code=500, detail=f"Data processing failed: {str(e)}")


@router.get("/{data_id}")
async def get_data_insights(data_id: str, session_id: str) -> DataInsightsResponse:
    """
    Retrieve insights for previously uploaded data

    Args:
        data_id: Data identifier
        session_id: Session identifier

    Returns:
        DataInsightsResponse with insights
    """
    logger = logging.getLogger(__name__)

    try:
        # Validate session
        session = await session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Check if data belongs to session
        if data_id not in session.data_uploads:
            raise HTTPException(status_code=404, detail="Data not found in session")

        # In a real implementation, you would retrieve the data from storage
        # For now, return a placeholder response
        return DataInsightsResponse(
            data_id=data_id,
            data_type=DataType.UNKNOWN,
            insights={"message": "Data insights retrieved"},
            confidence_score=0.8,
            processing_time_ms=0,
            anomalies_detected=[],
            recommendations=[],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to retrieve data insights: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve insights: {str(e)}"
        )


@router.get("/session/{session_id}/uploads")
async def list_session_uploads(session_id: str):
    """
    List all data uploads for a session

    Args:
        session_id: Session identifier

    Returns:
        List of uploaded data information
    """
    logger = logging.getLogger(__name__)

    try:
        # Validate session
        session = await session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Return session uploads
        return {
            "session_id": session_id,
            "uploads": session.data_uploads,
            "total_uploads": len(session.data_uploads),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list session uploads: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list uploads: {str(e)}")


@router.delete("/{data_id}")
async def delete_data(data_id: str, session_id: str):
    """
    Delete uploaded data

    Args:
        data_id: Data identifier
        session_id: Session identifier

    Returns:
        Success message
    """
    logger = logging.getLogger(__name__)

    try:
        # Validate session
        session = await session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Check if data belongs to session
        if data_id not in session.data_uploads:
            raise HTTPException(status_code=404, detail="Data not found in session")

        # Remove from session
        session.data_uploads.remove(data_id)

        # Add to investigation history
        await session_manager.add_investigation_history(
            session_id, {"action": "data_deletion", "data_id": data_id}
        )

        logger.info(f"Deleted data {data_id} from session {session_id}")

        return {"message": f"Data {data_id} deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete data: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete data: {str(e)}")
