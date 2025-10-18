"""
Unit tests for RunbookKnowledgeBase service.

Tests dual-source runbook similarity search and indexing.
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime
from typing import List, Dict

from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.models.report import (
    CaseReport,
    ReportType,
    ReportStatus,
    RunbookSource,
    RunbookMetadata,
    SimilarRunbook
)


@pytest.fixture
def mock_vector_store():
    """Create mock vector store."""
    store = Mock()
    store.add_documents = AsyncMock()
    store.query_by_embedding = AsyncMock()
    return store


@pytest.fixture
def runbook_kb(mock_vector_store):
    """Create RunbookKnowledgeBase instance with mocked dependencies."""
    return RunbookKnowledgeBase(vector_store=mock_vector_store)


@pytest.fixture
def sample_incident_runbook():
    """Create sample incident-driven runbook."""
    return CaseReport(
        report_id="report-123",
        case_id="case-abc",
        report_type=ReportType.RUNBOOK,
        title="Runbook: Database Connection Pool Exhaustion",
        content="# Runbook\n\n## Problem Description\n...",
        format="markdown",
        generation_status=ReportStatus.COMPLETED,
        generated_at="2025-10-13T10:30:00Z",
        generation_time_ms=12500,
        is_current=True,
        version=1,
        linked_to_closure=False,
        metadata=RunbookMetadata(
            source=RunbookSource.INCIDENT_DRIVEN,
            domain="database",
            tags=["postgresql", "connection-pool", "performance"],
            case_context={"root_cause": "Connection pool size too small"}
        )
    )


@pytest.fixture
def sample_document_runbook():
    """Create sample document-driven runbook."""
    return CaseReport(
        report_id="report-456",
        case_id="doc-derived",
        report_type=ReportType.RUNBOOK,
        title="Runbook: PostgreSQL Performance Troubleshooting",
        content="# Runbook\n\n## Problem Description\n...",
        format="markdown",
        generation_status=ReportStatus.COMPLETED,
        generated_at="2025-09-01T08:00:00Z",
        generation_time_ms=0,
        is_current=True,
        version=1,
        linked_to_closure=False,
        metadata=RunbookMetadata(
            source=RunbookSource.DOCUMENT_DRIVEN,
            domain="database",
            tags=["postgresql", "performance"],
            document_title="PostgreSQL Operations Guide",
            original_document_id="doc-789"
        )
    )


# =============================================================================
# Test: search_runbooks
# =============================================================================

@pytest.mark.asyncio
async def test_search_runbooks_high_similarity(runbook_kb, mock_vector_store, sample_incident_runbook):
    """Test searching for runbooks with high similarity match."""
    # Mock ChromaDB response
    mock_vector_store.query_by_embedding.return_value = {
        "ids": [["report-123"]],
        "distances": [[0.26]],  # Distance 0.26 → Similarity 0.87 (high)
        "metadatas": [[{
            "report_id": "report-123",
            "case_id": "case-abc",
            "case_title": "Database Connection Pool Exhaustion",
            "title": "Runbook: Database Connection Pool Exhaustion",
            "report_type": "runbook",
            "runbook_source": "incident_driven",
            "domain": "database",
            "tags": "postgresql,connection-pool,performance",
            "created_at": "2025-10-13T10:30:00Z"
        }]],
        "documents": [["# Runbook\n\n## Problem Description\n..."]]
    }

    # Search for similar runbooks
    query_embedding = [0.1] * 1024  # Dummy embedding
    results = await runbook_kb.search_runbooks(
        query_embedding=query_embedding,
        filters={"domain": "database"},
        top_k=5,
        min_similarity=0.65
    )

    # Assertions
    assert len(results) == 1
    assert isinstance(results[0], SimilarRunbook)
    assert results[0].similarity_score >= 0.85  # High similarity
    assert results[0].runbook.report_type == ReportType.RUNBOOK
    assert results[0].case_id == "case-abc"

    # Verify vector store was called correctly
    mock_vector_store.query_by_embedding.assert_called_once()
    call_args = mock_vector_store.query_by_embedding.call_args[1]
    assert call_args["query_embedding"] == query_embedding
    assert call_args["where"]["domain"] == "database"
    assert call_args["top_k"] == 5


@pytest.mark.asyncio
async def test_search_runbooks_no_results(runbook_kb, mock_vector_store):
    """Test searching when no similar runbooks exist."""
    # Mock empty ChromaDB response
    mock_vector_store.query_by_embedding.return_value = {
        "ids": [[]],
        "distances": [[]],
        "metadatas": [[]],
        "documents": [[]]
    }

    query_embedding = [0.1] * 1024
    results = await runbook_kb.search_runbooks(
        query_embedding=query_embedding,
        top_k=5,
        min_similarity=0.65
    )

    assert len(results) == 0


@pytest.mark.asyncio
async def test_search_runbooks_filters_by_similarity_threshold(runbook_kb, mock_vector_store):
    """Test that results below minimum similarity are filtered out."""
    # Mock response with low similarity (distance 1.0 → similarity 0.50)
    mock_vector_store.query_by_embedding.return_value = {
        "ids": [["report-123"]],
        "distances": [[1.0]],  # Low similarity
        "metadatas": [[{
            "report_id": "report-123",
            "case_id": "case-abc",
            "case_title": "Test Runbook",
            "title": "Runbook: Test",
            "report_type": "runbook",
            "runbook_source": "incident_driven",
            "domain": "general",
            "tags": "",
            "created_at": "2025-10-13T10:30:00Z"
        }]],
        "documents": [["Test content"]]
    }

    results = await runbook_kb.search_runbooks(
        query_embedding=[0.1] * 1024,
        min_similarity=0.65  # 65% threshold
    )

    # Result should be filtered out (similarity 0.50 < 0.65)
    assert len(results) == 0


@pytest.mark.asyncio
async def test_search_runbooks_sorts_by_similarity(runbook_kb, mock_vector_store):
    """Test that results are sorted by similarity score descending."""
    # Mock response with multiple results
    mock_vector_store.query_by_embedding.return_value = {
        "ids": [["report-1", "report-2", "report-3"]],
        "distances": [[0.4, 0.2, 0.6]],  # Similarities: 0.80, 0.90, 0.70
        "metadatas": [[
            {
                "report_id": "report-1",
                "case_id": "case-1",
                "case_title": "Database Case 1",
                "title": "Runbook: Database Issue 1",
                "report_type": "runbook",
                "runbook_source": "incident_driven",
                "domain": "general",
                "tags": "",
                "created_at": "2025-10-13T10:00:00Z"
            },
            {
                "report_id": "report-2",
                "case_id": "case-2",
                "case_title": "Database Case 2",
                "title": "Runbook: Database Issue 2",
                "report_type": "runbook",
                "runbook_source": "document_driven",
                "domain": "general",
                "tags": "",
                "created_at": "2025-10-13T11:00:00Z"
            },
            {
                "report_id": "report-3",
                "case_id": "case-3",
                "case_title": "Database Case 3",
                "title": "Runbook: Database Issue 3",
                "report_type": "runbook",
                "runbook_source": "incident_driven",
                "domain": "general",
                "tags": "",
                "created_at": "2025-10-13T12:00:00Z"
            }
        ]],
        "documents": [["Content 1", "Content 2", "Content 3"]]
    }

    results = await runbook_kb.search_runbooks(
        query_embedding=[0.1] * 1024,
        min_similarity=0.65
    )

    # Should be sorted: report-2 (0.90), report-1 (0.80), report-3 (0.70)
    assert len(results) == 3
    assert results[0].runbook.report_id == "report-2"
    assert results[0].similarity_score >= 0.89  # ~0.90
    assert results[1].runbook.report_id == "report-1"
    assert results[1].similarity_score >= 0.79  # ~0.80
    assert results[2].runbook.report_id == "report-3"
    assert results[2].similarity_score >= 0.69  # ~0.70


@pytest.mark.asyncio
async def test_search_runbooks_handles_error_gracefully(runbook_kb, mock_vector_store):
    """Test that search errors are handled gracefully."""
    # Mock vector store to raise exception
    mock_vector_store.query_by_embedding.side_effect = Exception("ChromaDB connection failed")

    results = await runbook_kb.search_runbooks(
        query_embedding=[0.1] * 1024
    )

    # Should return empty list instead of raising exception
    assert len(results) == 0


# =============================================================================
# Test: index_runbook
# =============================================================================

@pytest.mark.asyncio
async def test_index_runbook_incident_driven(runbook_kb, mock_vector_store, sample_incident_runbook):
    """Test indexing an incident-driven runbook."""
    await runbook_kb.index_runbook(
        runbook=sample_incident_runbook,
        source=RunbookSource.INCIDENT_DRIVEN,
        case_title="Database Connection Pool Exhaustion",
        domain="database",
        tags=["postgresql", "connection-pool", "performance"]
    )

    # Verify vector store was called
    mock_vector_store.add_documents.assert_called_once()
    call_args = mock_vector_store.add_documents.call_args[0][0]

    assert len(call_args) == 1
    doc = call_args[0]
    assert doc["id"] == "report-123"
    assert doc["content"] == sample_incident_runbook.content
    assert doc["metadata"]["report_type"] == "runbook"
    assert doc["metadata"]["runbook_source"] == "incident_driven"
    assert doc["metadata"]["domain"] == "database"
    assert doc["metadata"]["tags"] == "postgresql,connection-pool,performance"


@pytest.mark.asyncio
async def test_index_runbook_document_driven(runbook_kb, mock_vector_store, sample_document_runbook):
    """Test indexing a document-driven runbook."""
    await runbook_kb.index_runbook(
        runbook=sample_document_runbook,
        source=RunbookSource.DOCUMENT_DRIVEN,
        case_title="PostgreSQL Operations Guide",
        domain="database",
        tags=["postgresql", "performance"]
    )

    # Verify vector store was called
    mock_vector_store.add_documents.assert_called_once()
    call_args = mock_vector_store.add_documents.call_args[0][0]

    doc = call_args[0]
    assert doc["metadata"]["runbook_source"] == "document_driven"
    assert doc["metadata"]["document_title"] == "PostgreSQL Operations Guide"
    assert doc["metadata"]["original_document_id"] == "doc-789"


@pytest.mark.asyncio
async def test_index_runbook_skips_non_runbook_types(runbook_kb, mock_vector_store):
    """Test that non-runbook report types are not indexed."""
    incident_report = CaseReport(
        report_id="report-999",
        case_id="case-xyz",
        report_type=ReportType.INCIDENT_REPORT,  # Not a runbook
        title="Incident Report",
        content="# Incident Report...",
        format="markdown",
        generation_status=ReportStatus.COMPLETED,
        generated_at="2025-10-13T10:30:00Z",
        generation_time_ms=10000,
        is_current=True,
        version=1
    )

    await runbook_kb.index_runbook(
        runbook=incident_report,
        source=RunbookSource.INCIDENT_DRIVEN
    )

    # Should NOT call vector store for non-runbook types
    mock_vector_store.add_documents.assert_not_called()


# =============================================================================
# Test: index_document_derived_runbook
# =============================================================================

@pytest.mark.asyncio
async def test_index_document_derived_runbook(runbook_kb, mock_vector_store):
    """Test convenience method for indexing document-derived runbooks."""
    runbook_id = await runbook_kb.index_document_derived_runbook(
        runbook_content="# PostgreSQL Performance Runbook\n\n...",
        document_title="PostgreSQL Operations Guide",
        domain="database",
        tags=["postgresql", "performance", "troubleshooting"],
        original_document_id="doc-abc123"
    )

    # Should return a UUID
    assert isinstance(runbook_id, str)
    assert len(runbook_id) == 36  # UUID format

    # Verify vector store was called
    mock_vector_store.add_documents.assert_called_once()
    call_args = mock_vector_store.add_documents.call_args[0][0]

    doc = call_args[0]
    assert doc["id"] == runbook_id
    assert doc["metadata"]["case_id"] == "doc-derived"
    assert doc["metadata"]["runbook_source"] == "document_driven"
    assert doc["metadata"]["document_title"] == "PostgreSQL Operations Guide"
    assert doc["metadata"]["original_document_id"] == "doc-abc123"
    assert doc["metadata"]["domain"] == "database"
    assert "postgresql" in doc["metadata"]["tags"]


# =============================================================================
# Test: Dual-Source Runbook Architecture
# =============================================================================

@pytest.mark.asyncio
async def test_search_finds_both_incident_and_document_runbooks(runbook_kb, mock_vector_store):
    """Test that search returns runbooks from both sources."""
    # Mock response with mixed sources
    mock_vector_store.query_by_embedding.return_value = {
        "ids": [["report-incident", "report-document"]],
        "distances": [[0.2, 0.3]],  # Both high similarity
        "metadatas": [[
            {
                "report_id": "report-incident",
                "case_id": "case-abc",
                "case_title": "Incident Case",
                "title": "Runbook from Incident",
                "report_type": "runbook",
                "runbook_source": "incident_driven",
                "domain": "database",
                "tags": "postgresql",
                "created_at": "2025-10-13T10:00:00Z"
            },
            {
                "report_id": "report-document",
                "case_id": "doc-derived",
                "case_title": "PostgreSQL Guide",
                "title": "Runbook from Documentation",
                "report_type": "runbook",
                "runbook_source": "document_driven",
                "domain": "database",
                "tags": "postgresql",
                "created_at": "2025-09-01T08:00:00Z",
                "document_title": "PostgreSQL Guide"
            }
        ]],
        "documents": [["Incident runbook content", "Document runbook content"]]
    }

    results = await runbook_kb.search_runbooks(
        query_embedding=[0.1] * 1024,
        min_similarity=0.65
    )

    # Should find both types
    assert len(results) == 2

    # First result: incident-driven
    assert results[0].runbook.metadata.source == RunbookSource.INCIDENT_DRIVEN
    assert results[0].case_id == "case-abc"

    # Second result: document-driven
    assert results[1].runbook.metadata.source == RunbookSource.DOCUMENT_DRIVEN
    assert results[1].case_id == "doc-derived"
    assert results[1].runbook.metadata.document_title == "PostgreSQL Guide"
