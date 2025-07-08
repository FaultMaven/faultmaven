from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from faultmaven.main import app
from faultmaven.models import KnowledgeBaseDocument, SearchRequest

client = TestClient(app)


@pytest.fixture
def mock_kb_ingester():
    """Fixture to mock the KnowledgeIngester dependency."""
    mock = MagicMock()
    mock.get_job_status = AsyncMock()
    mock.ingest_document_object = AsyncMock()
    mock.search = AsyncMock()
    mock.get_document = AsyncMock()
    mock.delete_document = AsyncMock()
    mock.list_documents = AsyncMock()
    with patch("faultmaven.api.kb_management.kb_ingestion", mock):
        yield mock


def test_upload_document_ingestion_fails(mock_kb_ingester):
    """
    Test the scenario where document ingestion raises an exception.
    """
    mock_kb_ingester.ingest_document_object.side_effect = Exception("Ingestion failed")

    response = client.post(
        "/api/v1/kb/documents",
        files={"file": ("test.txt", b"content", "text/plain")},
        data={"title": "Test Document", "document_type": "guide"},
    )

    assert response.status_code == 500
    assert "Document upload failed" in response.json()["detail"]


def test_get_job_status_not_found(mock_kb_ingester):
    """
    Test getting the status of a job that does not exist.
    """
    mock_kb_ingester.get_job_status.return_value = None

    response = client.get("/api/v1/kb/jobs/non_existent_job")

    assert response.status_code == 404
    assert "Job not found" in response.json()["detail"]


def test_search_documents_fails(mock_kb_ingester):
    """
    Test the scenario where the search operation fails.
    """
    mock_kb_ingester.search.side_effect = Exception("Search failed")

    search_request = SearchRequest(query="test query")

    response = client.post("/api/v1/kb/search", json=search_request.model_dump())

    assert response.status_code == 500
    assert "Search failed" in response.json()["detail"]


def test_get_document_not_found(mock_kb_ingester):
    """
    Test retrieving a document that does not exist.
    """
    mock_kb_ingester.get_document.return_value = None
    response = client.get("/api/v1/kb/documents/non_existent_id")
    assert response.status_code == 404
    assert "Document not found" in response.json()["detail"]


def test_get_document_fails(mock_kb_ingester):
    """
    Test retrieving a document when the service fails.
    """
    mock_kb_ingester.get_document.side_effect = Exception("Service failed")
    response = client.get("/api/v1/kb/documents/any_id")
    assert response.status_code == 500
    assert "Failed to retrieve document" in response.json()["detail"]


def test_delete_document_not_found(mock_kb_ingester):
    """
    Test deleting a document that does not exist.
    """
    mock_kb_ingester.delete_document.return_value = False
    response = client.delete("/api/v1/kb/documents/non_existent_id")
    assert response.status_code == 404
    assert "Document not found" in response.json()["detail"]


def test_list_documents_fails(mock_kb_ingester):
    """
    Test listing documents when the service fails.
    """
    mock_kb_ingester.list_documents.side_effect = Exception("Service failed")
    response = client.get("/api/v1/kb/documents")
    assert response.status_code == 500
    assert "Failed to list documents" in response.json()["detail"]
