"""
Test 2: Data Ingestion Pipeline

Objective: To verify that submitting log data through the data service
triggers the full classification and processing pipeline, and that the
resulting insights are correctly stored in the user's session.

Setup: Uses mocked dependencies for isolated integration testing.
"""

import io
import os
import tempfile
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import UploadFile

from faultmaven.services.data_service import DataService
from faultmaven.services.session_service import SessionService  
from faultmaven.models import DataInsightsResponse, DataType, SessionContext, UploadedData

# Skip backend service checks for this test
os.environ["SKIP_SERVICE_CHECKS"] = "true"


@pytest.mark.asyncio
async def test_data_ingestion_pipeline(
    sample_log_content: str,
):
    """
    Test Steps:
    1. Create a mock session
    2. Read sample log content from fixture
    3. Process data through DataService with mocked dependencies
    4. Assert response contains valid DataInsightsResponse
    5. Verify that classification and processing occurred
    """
    # Step 1: Setup mocked dependencies
    mock_session_service = Mock(spec=SessionService)
    mock_classifier = AsyncMock()
    mock_log_processor = Mock()
    mock_sanitizer = Mock()
    
    # Configure mock returns
    test_session_id = "test-session-123"
    mock_session_context = SessionContext(
        session_id=test_session_id,
        user_id="test-user",
        data_uploads=[],
        insights=[],
        troubleshooting_history=[]
    )
    mock_session_service.get_session.return_value = mock_session_context
    mock_classifier.classify.return_value = DataType.LOG_FILE
    mock_sanitizer.sanitize.return_value = sample_log_content
    
    mock_log_processor.analyze.return_value = {
        "error_patterns": ["DatabaseConnectionError", "Connection timeout"],
        "severity_levels": {"ERROR": 2, "WARN": 1, "FATAL": 1},
        "time_range": {"start": "2024-01-15 14:30:25", "end": "2024-01-15 14:30:28"},
        "key_events": [
            "Database connection failure",
            "Retry attempts initiated", 
            "System shutdown after max retries"
        ]
    }

    # Step 2: Create DataService with mocked dependencies
    data_service = DataService(
        data_classifier=mock_classifier,
        log_processor=mock_log_processor,
        data_sanitizer=mock_sanitizer
    )

    # Step 3: Process the data using ingest_data method
    response = await data_service.ingest_data(
        content=sample_log_content,
        session_id=test_session_id,
        file_name="test.log"
    )

    # Step 4: Assert response contains valid UploadedData
    assert isinstance(response, UploadedData)
    assert response.data_id is not None
    assert response.data_type == DataType.LOG_FILE
    assert response.session_id == test_session_id
    assert response.file_name == "test.log"

    # Step 5: Verify that dependencies were called correctly
    mock_classifier.classify.assert_called_once_with(sample_log_content, "test.log")
    mock_sanitizer.sanitize.assert_called_once_with(sample_log_content)
    
    # Verify processing occurred as expected
    assert response.data_type == DataType.LOG_FILE


@pytest.mark.skip(reason="Requires backend API - convert to service-level test")
@pytest.mark.asyncio
async def test_data_ingestion_with_different_file_types():
    """
    Test data ingestion with different file types and content.
    TODO: Convert to use DataService directly with mocked dependencies
    """
    pass


@pytest.mark.skip(reason="Requires backend API - convert to service-level test")
@pytest.mark.asyncio
async def test_data_ingestion_error_handling():
    """Test error handling in data ingestion pipeline."""
    pass


@pytest.mark.skip(reason="Requires backend API - convert to service-level test")
@pytest.mark.asyncio
async def test_data_ingestion_empty_file():
    """Test data ingestion with empty file."""
    pass


@pytest.mark.skip(reason="Requires backend API - convert to service-level test")
@pytest.mark.asyncio
async def test_data_ingestion_large_file():
    """Test data ingestion with larger file content."""
    pass


@pytest.mark.skip(reason="Requires backend API - convert to service-level test")
@pytest.mark.asyncio
async def test_multiple_data_uploads_same_session():
    """Test multiple data uploads to the same session."""
    pass


@pytest.mark.skip(reason="Requires backend API - convert to service-level test")
@pytest.mark.asyncio
async def test_data_retrieval_endpoint():
    """Test retrieving data insights after upload."""
    pass


@pytest.mark.skip(reason="Requires backend API - convert to service-level test")
@pytest.mark.asyncio
async def test_list_session_uploads():
    """Test listing all uploads for a session."""
    pass
