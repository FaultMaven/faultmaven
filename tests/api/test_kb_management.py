from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.models_original import KnowledgeBaseDocument, SearchRequest
from faultmaven.api.v1.routes.knowledge import router, get_kb_ingestion

app = FastAPI()
app.include_router(router)
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
    app.dependency_overrides[get_kb_ingestion] = lambda: mock
    yield mock
    # Clean up dependency override
    app.dependency_overrides.pop(get_kb_ingestion, None)


def test_upload_document_ingestion_fails(mock_kb_ingester):
    """
    Test the scenario where document ingestion raises an exception.
    """
    mock_kb_ingester.ingest_document_object.side_effect = Exception("Ingestion failed")

    response = client.post(
        "/kb/documents",
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

    response = client.get("/kb/jobs/non_existent_job")

    assert response.status_code == 404
    assert "Job not found" in response.json()["detail"]


def test_search_documents_fails(mock_kb_ingester):
    """
    Test the scenario where the search operation fails.
    """
    mock_kb_ingester.search.side_effect = Exception("Search failed")

    search_request = SearchRequest(query="test query")

    response = client.post("/kb/search", json=search_request.model_dump())

    assert response.status_code == 500
    assert "Search failed" in response.json()["detail"]


def test_get_document_not_found(mock_kb_ingester):
    """
    Test retrieving a document that does not exist.
    """
    mock_kb_ingester.get_document.return_value = None
    response = client.get("/kb/documents/non_existent_id")
    assert response.status_code == 404
    assert "Document not found" in response.json()["detail"]


def test_get_document_fails(mock_kb_ingester):
    """
    Test retrieving a document when the service fails.
    """
    mock_kb_ingester.get_document.side_effect = Exception("Service failed")
    response = client.get("/kb/documents/any_id")
    assert response.status_code == 500
    assert "Failed to retrieve document" in response.json()["detail"]


def test_delete_document_not_found(mock_kb_ingester):
    """
    Test deleting a document that does not exist.
    """
    mock_kb_ingester.delete_document.return_value = False
    response = client.delete("/kb/documents/non_existent_id")
    assert response.status_code == 404
    assert "Document not found" in response.json()["detail"]


def test_list_documents_fails(mock_kb_ingester):
    """
    Test listing documents when the service fails.
    """
    mock_kb_ingester.list_documents.side_effect = Exception("Service failed")
    response = client.get("/kb/documents")
    assert response.status_code == 500
    assert "Failed to list documents" in response.json()["detail"]
