import pytest
import io
from unittest.mock import Mock, AsyncMock
from fastapi.testclient import TestClient
from httpx import AsyncClient
from faultmaven.api.data_ingestion import router
from faultmaven.models import DataType, DataInsightsResponse
import pytest_asyncio


class TestDataIngestionAPI:
    """Test suite for data ingestion API endpoints."""

    @pytest.fixture
    def mock_session_manager(self):
        """Mock SessionManager dependency."""
        return Mock()

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
    def test_app(self, mock_session_manager, mock_data_classifier, 
                 mock_log_processor, mock_data_sanitizer):
        """Create test FastAPI app with mocked dependencies."""
        from fastapi import FastAPI
        from faultmaven.api import data_ingestion
        app = FastAPI()
        app.include_router(data_ingestion.router)
        # Dependency overrides for DI providers
        app.dependency_overrides[data_ingestion.get_session_manager] = lambda: mock_session_manager
        app.dependency_overrides[data_ingestion.get_data_classifier] = lambda: mock_data_classifier
        app.dependency_overrides[data_ingestion.get_log_processor] = lambda: mock_log_processor
        return app

    @pytest.fixture
    def client(self, test_app):
        """Create test client."""
        return TestClient(test_app)

    def test_upload_data_success(self, client, mock_session_manager, 
                                 mock_data_classifier, mock_log_processor, 
                                 mock_data_sanitizer):
        """Test successful data upload."""
        # Mock service responses
        mock_data_classifier.classify = AsyncMock(return_value=DataType.LOG_FILE)
        mock_data_sanitizer.sanitize.return_value = "Sanitized content"
        mock_log_processor.process = AsyncMock(return_value=DataInsightsResponse(
            data_id="test-data-id",
            data_type=DataType.LOG_FILE,
            insights={"error_count": 2, "error_rate": 0.4},
            confidence_score=1.0,
            processing_time_ms=10,
            anomalies_detected=[],
            recommendations=["Investigate errors"]
        ))
        mock_session_manager.create_session.return_value = "test-session-id"
        mock_session_manager.update_session.return_value = None

        # Create test file content
        file_content = b"2024-01-01 12:00:00 ERROR Test error\n2024-01-01 12:00:01 INFO Test info"
        
        response = client.post(
            "/data",
            files={"file": ("test.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"}
        )
        
        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data["data_type"] == "log_file"
        assert "error_count" in data["insights"]

    def test_upload_data_no_file(self, client):
        """Test upload without file."""
        response = client.post(
            "/data",
            data={"session_id": "test-session-id"}
        )
        assert response.status_code == 422  # Validation error

    def test_upload_data_empty_file(self, client, mock_session_manager, 
                                    mock_data_classifier, mock_log_processor, 
                                    mock_data_sanitizer):
        """Test upload with empty file."""
        # Mock service responses
        mock_data_classifier.classify = AsyncMock(return_value=DataType.LOG_FILE)
        mock_data_sanitizer.sanitize.return_value = ""
        mock_log_processor.process = AsyncMock(return_value=DataInsightsResponse(
            data_id="test-data-id",
            data_type=DataType.LOG_FILE,
            insights={"error_count": 0, "error_rate": 0.0},
            confidence_score=1.0,
            processing_time_ms=10,
            anomalies_detected=[],
            recommendations=["No action needed"]
        ))
        mock_session_manager.create_session.return_value = "test-session-id"

        # Create empty file
        file_content = b""
        
        response = client.post(
            "/data",
            files={"file": ("empty.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["insights"]["error_count"] == 0

    def test_upload_data_classification_failure(self, client, 
                                                mock_data_classifier):
        """Test handling of classification failure."""
        mock_data_classifier.classify.side_effect = Exception("Classification error")
        file_content = b"Test log content"
        response = client.post(
            "/data",
            files={"file": ("test.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"}
        )
        assert response.status_code == 500
        assert "detail" in response.json()

    def test_upload_data_processing_failure(self, client, 
                                             mock_data_classifier, 
                                             mock_log_processor):
        """Test handling of processing failure."""
        mock_data_classifier.classify = AsyncMock(return_value=DataType.LOG_FILE)
        mock_log_processor.process.side_effect = Exception("Processing error")
        file_content = b"Test log content"
        response = client.post(
            "/data",
            files={"file": ("test.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"}
        )
        assert response.status_code == 500
        assert "detail" in response.json()

    def test_upload_data_session_creation_failure(self, client, 
                                                  mock_session_manager):
        """Test handling of session creation failure."""
        mock_session_manager.create_session.side_effect = Exception("Session error")
        file_content = b"Test log content"
        response = client.post(
            "/data",
            files={"file": ("test.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"}
        )
        assert response.status_code == 500
        assert "detail" in response.json()

    def test_upload_data_different_file_types(self, client, 
                                               mock_session_manager, 
                                               mock_data_classifier, 
                                               mock_log_processor, 
                                               mock_data_sanitizer):
        """Test upload with different file types."""
        # Mock responses
        mock_data_classifier.classify = AsyncMock(return_value=DataType.LOG_FILE)
        mock_data_sanitizer.sanitize.return_value = "Sanitized content"
        mock_log_processor.process = AsyncMock(return_value=DataInsightsResponse(
            data_id="test-data-id",
            data_type=DataType.LOG_FILE,
            insights={"error_count": 1, "error_rate": 0.1},
            confidence_score=1.0,
            processing_time_ms=10,
            anomalies_detected=[],
            recommendations=["Review logs"]
        ))
        mock_session_manager.create_session.return_value = "test-session-id"
        file_content = b"Test application log content"
        response = client.post(
            "/data",
            files={"file": ("app.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["data_type"] == "log_file"

    def test_upload_data_large_file(self, client, mock_session_manager, 
                                    mock_data_classifier, mock_log_processor, 
                                    mock_data_sanitizer):
        """Test upload with large file."""
        # Mock responses
        mock_data_classifier.classify = AsyncMock(return_value=DataType.LOG_FILE)
        mock_data_sanitizer.sanitize.return_value = "Sanitized content"
        mock_log_processor.process = AsyncMock(return_value=DataInsightsResponse(
            data_id="test-data-id",
            data_type=DataType.LOG_FILE,
            insights={"error_count": 100, "error_rate": 0.5},
            confidence_score=1.0,
            processing_time_ms=10,
            anomalies_detected=[],
            recommendations=["Investigate high error rate"]
        ))
        mock_session_manager.create_session.return_value = "test-session-id"
        file_content = b"Error\n" * 1000
        response = client.post(
            "/data",
            files={"file": ("large.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["insights"]["error_count"] == 100

    def test_upload_data_with_anomalies(self, client, mock_session_manager, 
                                        mock_data_classifier, mock_log_processor, 
                                        mock_data_sanitizer):
        """Test upload with detected anomalies."""
        # Mock responses with anomalies
        mock_data_classifier.classify = AsyncMock(return_value=DataType.LOG_FILE)
        mock_data_sanitizer.sanitize.return_value = "Sanitized content"
        mock_log_processor.process = AsyncMock(return_value=DataInsightsResponse(
            data_id="test-data-id",
            data_type=DataType.LOG_FILE,
            insights={"error_count": 2, "error_rate": 0.2},
            confidence_score=1.0,
            processing_time_ms=10,
            anomalies_detected=[{"message": "Spike in errors"}],
            recommendations=["Investigate spike"]
        ))
        mock_session_manager.create_session.return_value = "test-session-id"
        file_content = b"Error\n" * 10
        response = client.post(
            "/data",
            files={"file": ("anomaly.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["anomalies_detected"] == [{"message": "Spike in errors"}]

    def test_upload_data_sanitization(self, client, mock_session_manager, 
                                      mock_data_classifier, mock_log_processor, 
                                      mock_data_sanitizer):
        """Test that data is properly sanitized."""
        # Mock sanitization
        mock_data_classifier.classify = AsyncMock(return_value=DataType.LOG_FILE)
        mock_data_sanitizer.sanitize.return_value = "Sanitized content"
        mock_log_processor.process = AsyncMock(return_value=DataInsightsResponse(
            data_id="test-data-id",
            data_type=DataType.LOG_FILE,
            insights={"error_count": 1, "error_rate": 0.1},
            confidence_score=1.0,
            processing_time_ms=10,
            anomalies_detected=[],
            recommendations=["Review sanitized logs"]
        ))
        mock_session_manager.create_session.return_value = "test-session-id"
        file_content = b"Sensitive info\n"
        response = client.post(
            "/data",
            files={"file": ("sanitized.log", io.BytesIO(file_content), "text/plain")},
            data={"session_id": "test-session-id"}
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