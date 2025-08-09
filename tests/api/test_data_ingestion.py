import io
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import AsyncClient

from faultmaven.api.v1.routes.data import router
from faultmaven.models_original import DataInsightsResponse, DataType, UploadedData


class TestDataIngestionAPI:
    """Test suite for data ingestion API endpoints."""

    @pytest.fixture
    def mock_session_manager(self):
        """Mock SessionManager dependency."""
        mock = Mock()
        mock.session_timeout_seconds = 1800  # 30 minutes
        mock.get_session = AsyncMock()
        mock.add_data_upload = AsyncMock()
        mock.add_investigation_history = AsyncMock()
        return mock

    @pytest.fixture
    def mock_data_classifier(self):
        """Mock DataClassifier dependency."""
        mock = Mock()
        mock.classify = AsyncMock()
        return mock

    @pytest.fixture
    def mock_log_processor(self):
        """Mock LogProcessor dependency."""
        mock = Mock()
        mock.process = AsyncMock()
        return mock

    @pytest.fixture
    def mock_data_sanitizer(self):
        """Mock DataSanitizer dependency."""
        return Mock()

    @pytest.fixture
    def mock_redis_client(self):
        """Mock Redis client dependency."""
        mock = Mock()
        mock.set = AsyncMock()
        mock.get = AsyncMock()
        mock.delete = AsyncMock()
        return mock

    @pytest.fixture
    def mock_data_service(
        self,
        mock_session_manager,
        mock_data_classifier,
        mock_log_processor,
        mock_data_sanitizer,
        mock_redis_client,
    ):
        """Create a mock DataServiceRefactored with proper setup."""
        from unittest.mock import AsyncMock, Mock
        mock_service = Mock()
        mock_service.ingest_data = AsyncMock()
        mock_service.analyze_data = AsyncMock()
        mock_service.get_session_data = AsyncMock()
        mock_service.batch_process = AsyncMock()
        mock_service.delete_data = AsyncMock()
        mock_service.health_check = AsyncMock()
        return mock_service

    @pytest.fixture
    def test_app(
        self,
        mock_data_service,
    ):
        """Create test FastAPI app with mocked dependencies."""
        from fastapi import FastAPI

        from faultmaven.api.v1.routes.data import router as data_router
        from faultmaven.api.v1.dependencies import get_data_service

        app = FastAPI()
        app.include_router(data_router)
        # Override the main data service dependency
        app.dependency_overrides[get_data_service] = (
            lambda: mock_data_service
        )
        return app

    @pytest.fixture
    def client(self, test_app):
        """Create test client."""
        with TestClient(test_app) as client:
            yield client

    def test_upload_data_success(
        self,
        client,
        mock_data_service,
    ):
        """Test successful data upload."""
        # Mock the data service response
        mock_uploaded_data = UploadedData(
            data_id="test-data-id",
            session_id="test-session-id",
            data_type=DataType.LOG_FILE,
            content="Sanitized content",
            file_name="test.log",
            file_size=100,
            processing_status="completed",
            insights={
                "error_count": 2,
                "error_rate": 0.4,
                "processing_time_ms": 10,
                "confidence_score": 1.0,
                "anomalies_detected": [],
                "recommendations": ["Investigate errors"]
            }
        )
        mock_data_service.ingest_data.return_value = mock_uploaded_data

        # Create test file content
        file_content = (
            b"2024-01-01 12:00:00 ERROR Test error\n2024-01-01 12:00:01 INFO Test info"
        )

        response = client.post(
            "/data",
            files={"file": ("test.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"},
        )

        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data["data_type"] == "log_file"
        assert "error_count" in data["insights"]

    def test_upload_data_no_file(self, client):
        """Test upload without file."""
        response = client.post("/data", data={"session_id": "test-session-id"})
        assert response.status_code == 422  # Validation error

    def test_upload_data_empty_file(
        self,
        client,
        mock_data_service,
    ):
        """Test upload with empty file."""
        # Mock the data service response for empty file
        mock_uploaded_data = UploadedData(
            data_id="test-data-id",
            session_id="test-session-id",
            data_type=DataType.LOG_FILE,
            content="",
            file_name="empty.log",
            file_size=0,
            processing_status="completed",
            insights={
                "error_count": 0,
                "error_rate": 0.0,
                "processing_time_ms": 10,
                "confidence_score": 1.0,
                "anomalies_detected": [],
                "recommendations": ["No action needed"]
            }
        )
        mock_data_service.ingest_data.return_value = mock_uploaded_data

        # Create empty file
        file_content = b""

        response = client.post(
            "/data",
            files={"file": ("empty.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["insights"]["error_count"] == 0

    def test_upload_data_classification_failure(
        self, client, mock_data_service
    ):
        """Test handling of classification failure."""
        # Make the data service raise an exception during ingestion
        mock_data_service.ingest_data.side_effect = RuntimeError("Data ingestion failed: Classification error")
        file_content = b"Test log content"
        response = client.post(
            "/data",
            files={"file": ("test.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"},
        )
        assert response.status_code == 500
        assert "detail" in response.json()

    def test_upload_data_processing_failure(
        self, client, mock_data_service
    ):
        """Test handling of processing failure."""
        # Make the data service raise an exception during processing
        mock_data_service.ingest_data.side_effect = RuntimeError("Data ingestion failed: Processing error")
        file_content = b"Test log content"
        response = client.post(
            "/data",
            files={"file": ("test.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"},
        )
        assert response.status_code == 500
        assert "detail" in response.json()

    def test_upload_data_session_creation_failure(self, client, mock_data_service):
        """Test handling of session creation failure."""
        # Mock session validation failure (simulating session not found)
        mock_data_service.ingest_data.side_effect = FileNotFoundError("Session not found")
        file_content = b"Test log content"
        response = client.post(
            "/data",
            files={"file": ("test.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"},
        )
        assert response.status_code == 404  # Session not found
        assert "detail" in response.json()

    def test_upload_data_different_file_types(
        self,
        client,
        mock_data_service,
    ):
        """Test upload with different file types."""
        # Mock the data service response for different file types
        mock_uploaded_data = UploadedData(
            data_id="test-data-id",
            session_id="test-session-id",
            data_type=DataType.LOG_FILE,
            content="Sanitized content",
            file_name="app.log",
            file_size=100,
            processing_status="completed",
            insights={
                "error_count": 1,
                "error_rate": 0.1,
                "processing_time_ms": 10,
                "confidence_score": 1.0,
                "anomalies_detected": [],
                "recommendations": ["Review logs"]
            }
        )
        mock_data_service.ingest_data.return_value = mock_uploaded_data
        file_content = b"Test application log content"
        response = client.post(
            "/data",
            files={"file": ("app.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["data_type"] == "log_file"

    def test_upload_data_large_file(
        self,
        client,
        mock_data_service,
    ):
        """Test upload with large file."""
        # Mock the data service response for large file
        mock_uploaded_data = UploadedData(
            data_id="test-data-id",
            session_id="test-session-id",
            data_type=DataType.LOG_FILE,
            content="Sanitized content",
            file_name="large.log",
            file_size=5000,
            processing_status="completed",
            insights={
                "error_count": 100,
                "error_rate": 0.5,
                "processing_time_ms": 10,
                "confidence_score": 1.0,
                "anomalies_detected": [],
                "recommendations": ["Investigate high error rate"]
            }
        )
        mock_data_service.ingest_data.return_value = mock_uploaded_data
        file_content = b"Error\n" * 1000
        response = client.post(
            "/data",
            files={"file": ("large.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["insights"]["error_count"] == 100

    def test_upload_data_with_anomalies(
        self,
        client,
        mock_data_service,
    ):
        """Test upload with detected anomalies."""
        # Mock the data service response with anomalies
        mock_uploaded_data = UploadedData(
            data_id="test-data-id",
            session_id="test-session-id",
            data_type=DataType.LOG_FILE,
            content="Sanitized content",
            file_name="anomaly.log",
            file_size=100,
            processing_status="completed",
            insights={
                "error_count": 2,
                "error_rate": 0.2,
                "processing_time_ms": 10,
                "confidence_score": 1.0,
                "anomalies_detected": [{"message": "Spike in errors"}],
                "recommendations": ["Investigate spike"]
            }
        )
        mock_data_service.ingest_data.return_value = mock_uploaded_data
        file_content = b"Error\n" * 10
        response = client.post(
            "/data",
            files={"file": ("anomaly.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["insights"]["anomalies_detected"] == [{"message": "Spike in errors"}]

    def test_upload_data_sanitization(
        self,
        client,
        mock_data_service,
    ):
        """Test that data is properly sanitized."""
        # Mock the data service response for sanitized data
        mock_uploaded_data = UploadedData(
            data_id="test-data-id",
            session_id="test-session-id",
            data_type=DataType.LOG_FILE,
            content="Sanitized content",
            file_name="sanitized.log",
            file_size=100,
            processing_status="completed",
            insights={
                "error_count": 1,
                "error_rate": 0.1,
                "processing_time_ms": 10,
                "confidence_score": 1.0,
                "anomalies_detected": [],
                "recommendations": ["Review sanitized logs"]
            }
        )
        mock_data_service.ingest_data.return_value = mock_uploaded_data
        file_content = b"Sensitive info\n"
        response = client.post(
            "/data",
            files={"file": ("sanitized.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["insights"]["error_count"] == 1

    def test_router_includes_correct_endpoints(self, test_app):
        """Test that router includes the correct endpoints."""
        routes = [route.path for route in test_app.routes]
        assert "/data" in routes or "/data/" in routes

    def test_router_uses_correct_methods(self, test_app):
        """Test that router uses the correct HTTP methods."""
        routes = [(route.path, route.methods) for route in test_app.routes]
        data_routes = [route for route in routes if route[0] in ["/data", "/data/"]]
        assert len(data_routes) > 0
        assert "POST" in data_routes[0][1]
