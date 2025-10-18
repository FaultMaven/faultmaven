"""Test module for Redis report store implementation.

This module tests the RedisReportStore class which implements IReportStore interface,
focusing on report persistence, versioning, and the data lifecycle strategy.

Tests cover:
- Report storage and retrieval
- Report versioning (up to 5 versions per type)
- Data lifecycle: runbooks independent, reports cascade delete
- Report linking to case closure
- ChromaDB integration for content storage
- Concurrency scenarios

Data Lifecycle Strategy Tested:
- RUNBOOK: Independent, persists beyond case lifecycle
- INCIDENT_REPORT: Cascade delete with case
- POST_MORTEM: Cascade delete with case
"""

import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch, MagicMock
from typing import List, Dict, Any

from faultmaven.infrastructure.persistence.redis_report_store import RedisReportStore
from faultmaven.models.report import (
    CaseReport,
    ReportType,
    ReportStatus,
    RunbookSource,
    RunbookMetadata
)
from faultmaven.exceptions import ServiceException


class MockRedisClient:
    """Mock Redis client for testing"""

    def __init__(self):
        self.data = {}
        self.sorted_sets = {}
        self.hashes = {}

        # Mock methods
        self.hset = AsyncMock(return_value=True)
        self.hget = AsyncMock(return_value=None)
        self.hgetall = AsyncMock(return_value={})
        self.hdel = AsyncMock(return_value=1)
        self.delete = AsyncMock(return_value=1)
        self.exists = AsyncMock(return_value=False)
        self.zadd = AsyncMock(return_value=1)
        self.zrange = AsyncMock(return_value=[])
        self.zrem = AsyncMock(return_value=1)
        self.zrevrange = AsyncMock(return_value=[])
        self.pipeline = Mock()

        # Setup pipeline mock
        pipeline_mock = Mock()
        pipeline_mock.hset = Mock(return_value=pipeline_mock)
        pipeline_mock.hdel = Mock(return_value=pipeline_mock)
        pipeline_mock.delete = Mock(return_value=pipeline_mock)
        pipeline_mock.zadd = Mock(return_value=pipeline_mock)
        pipeline_mock.zrem = Mock(return_value=pipeline_mock)
        pipeline_mock.execute = AsyncMock(return_value=[True] * 10)
        self.pipeline.return_value = pipeline_mock
        self.pipeline_instance = pipeline_mock


class MockVectorStore:
    """Mock ChromaDB vector store for testing"""

    def __init__(self):
        self.documents = {}
        self.add_documents = AsyncMock()
        self.delete_documents = AsyncMock()
        self.query_by_embedding = AsyncMock(return_value={
            "documents": [["Mock report content"]],
            "ids": [["report-123"]]
        })


class MockRunbookKB:
    """Mock RunbookKnowledgeBase for testing"""

    def __init__(self):
        self.index_runbook = AsyncMock()


@pytest.fixture
def mock_redis_client():
    """Fixture providing mock Redis client"""
    return MockRedisClient()


@pytest.fixture
def mock_vector_store():
    """Fixture providing mock vector store"""
    return MockVectorStore()


@pytest.fixture
def mock_runbook_kb():
    """Fixture providing mock runbook KB"""
    return MockRunbookKB()


@pytest.fixture
def report_store(mock_redis_client, mock_vector_store, mock_runbook_kb):
    """Fixture providing RedisReportStore instance"""
    return RedisReportStore(
        redis_client=mock_redis_client,
        vector_store=mock_vector_store,
        runbook_kb=mock_runbook_kb
    )


@pytest.fixture
def sample_report():
    """Fixture providing sample report"""
    return CaseReport(
        report_id="report-123",
        case_id="case-456",
        report_type=ReportType.INCIDENT_REPORT,
        title="Sample Incident Report: Database Connection Failure",
        content="# Incident Report\n\nRoot cause analysis...",
        format="markdown",
        generation_status=ReportStatus.COMPLETED,
        generated_at=datetime.utcnow().isoformat() + 'Z',
        generation_time_ms=5000,
        is_current=True,
        version=1,
        linked_to_closure=False,
        metadata=None
    )


@pytest.fixture
def sample_runbook():
    """Fixture providing sample runbook report"""
    return CaseReport(
        report_id="runbook-789",
        case_id="case-456",
        report_type=ReportType.RUNBOOK,
        title="Database Connection Troubleshooting Runbook",
        content="# Runbook\n\nStep-by-step resolution...",
        format="markdown",
        generation_status=ReportStatus.COMPLETED,
        generated_at=datetime.utcnow().isoformat() + 'Z',
        generation_time_ms=7000,
        is_current=True,
        version=1,
        linked_to_closure=False,
        metadata=RunbookMetadata(
            source=RunbookSource.INCIDENT_DRIVEN,
            tags=["database", "connection"],
            domain="infrastructure"
        )
    )


# ============================================================================
# REPORT STORAGE TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_save_report_success(report_store, sample_report, mock_redis_client, mock_vector_store):
    """Test successful report save operation"""
    # Arrange
    mock_redis_client.hgetall.return_value = {}  # No previous report

    # Act
    result = await report_store.save_report(sample_report)

    # Assert
    assert result is True
    assert mock_vector_store.add_documents.called
    assert mock_redis_client.pipeline.called


@pytest.mark.asyncio
async def test_save_report_marks_previous_as_not_current(report_store, sample_report, mock_redis_client):
    """Test that saving new report marks previous version as not current"""
    # Arrange
    # Simulate previous version exists
    mock_redis_client.hgetall.return_value = {
        b"report_id": b"old-report-123",
        b"is_current": b"true"
    }

    # Act
    result = await report_store.save_report(sample_report)

    # Assert
    assert result is True
    pipeline = mock_redis_client.pipeline_instance
    # Should have called hset to mark previous as not current
    assert pipeline.hset.called


@pytest.mark.asyncio
async def test_save_runbook_auto_indexes_in_kb(report_store, sample_runbook, mock_runbook_kb):
    """Test that saving runbook automatically indexes it in RunbookKB"""
    # Act
    result = await report_store.save_report(sample_runbook)

    # Assert
    assert result is True
    assert mock_runbook_kb.index_runbook.called


@pytest.mark.asyncio
async def test_get_report_success(report_store, sample_report, mock_redis_client, mock_vector_store):
    """Test successful report retrieval"""
    # Arrange
    mock_redis_client.hgetall.return_value = {
        b"report_id": b"report-123",
        b"case_id": b"case-456",
        b"report_type": b"incident_report",
        b"title": b"Sample Report",
        b"format": b"markdown",
        b"generation_status": b"completed",
        b"generated_at": datetime.utcnow().isoformat().encode(),
        b"generation_time_ms": b"5000",
        b"is_current": b"true",
        b"version": b"1",
        b"linked_to_closure": b"false",
        b"metadata_json": b"null"
    }
    mock_vector_store.query_by_embedding.return_value = {
        "documents": [["# Sample Report Content"]],
        "ids": [["report-123"]]
    }

    # Act
    result = await report_store.get_report("report-123")

    # Assert
    assert result is not None
    assert result.report_id == "report-123"
    assert result.case_id == "case-456"
    assert result.content == "# Sample Report Content"


@pytest.mark.asyncio
async def test_get_report_not_found(report_store, mock_redis_client):
    """Test retrieving non-existent report"""
    # Arrange
    mock_redis_client.hgetall.return_value = {}

    # Act
    result = await report_store.get_report("nonexistent-report")

    # Assert
    assert result is None


# ============================================================================
# REPORT VERSIONING TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_get_case_reports_current_only(report_store, mock_redis_client, mock_vector_store):
    """Test retrieving only current reports for a case"""
    # Arrange
    mock_redis_client.hgetall.side_effect = [
        # Current incident report
        {
            b"report_id": b"report-v2",
            b"case_id": b"case-456",
            b"report_type": b"incident_report",
            b"title": b"Incident Report v2",
            b"format": b"markdown",
            b"generation_status": b"completed",
            b"generated_at": datetime.utcnow().isoformat().encode(),
            b"generation_time_ms": b"5000",
            b"is_current": b"true",
            b"version": b"2",
            b"linked_to_closure": b"false",
            b"metadata_json": b"null"
        }
    ]
    mock_redis_client.hget.return_value = b"report-v2"
    mock_vector_store.query_by_embedding.return_value = {
        "documents": [["Content v2"]],
        "ids": [["report-v2"]]
    }

    # Act
    result = await report_store.get_case_reports("case-456", include_history=False)

    # Assert
    assert len(result) == 1
    assert result[0].version == 2
    assert result[0].is_current is True


@pytest.mark.asyncio
async def test_get_case_reports_with_history(report_store, mock_redis_client, mock_vector_store):
    """Test retrieving all report versions (history)"""
    # Arrange
    mock_redis_client.zrange.return_value = [b"report-v1", b"report-v2", b"report-v3"]
    mock_redis_client.hgetall.side_effect = [
        # Version 1
        {
            b"report_id": b"report-v1",
            b"version": b"1",
            b"is_current": b"false",
            b"case_id": b"case-456",
            b"report_type": b"incident_report",
            b"title": b"Report v1",
            b"format": b"markdown",
            b"generation_status": b"completed",
            b"generated_at": datetime.utcnow().isoformat().encode(),
            b"generation_time_ms": b"5000",
            b"linked_to_closure": b"false",
            b"metadata_json": b"null"
        },
        # Version 2
        {
            b"report_id": b"report-v2",
            b"version": b"2",
            b"is_current": b"false",
            b"case_id": b"case-456",
            b"report_type": b"incident_report",
            b"title": b"Report v2",
            b"format": b"markdown",
            b"generation_status": b"completed",
            b"generated_at": datetime.utcnow().isoformat().encode(),
            b"generation_time_ms": b"5000",
            b"linked_to_closure": b"false",
            b"metadata_json": b"null"
        },
        # Version 3 (current)
        {
            b"report_id": b"report-v3",
            b"version": b"3",
            b"is_current": b"true",
            b"case_id": b"case-456",
            b"report_type": b"incident_report",
            b"title": b"Report v3",
            b"format": b"markdown",
            b"generation_status": b"completed",
            b"generated_at": datetime.utcnow().isoformat().encode(),
            b"generation_time_ms": b"5000",
            b"linked_to_closure": b"false",
            b"metadata_json": b"null"
        }
    ]

    # Act
    result = await report_store.get_case_reports("case-456", include_history=True)

    # Assert
    assert len(result) == 3
    assert result[0].version == 1
    assert result[1].version == 2
    assert result[2].version == 3
    assert result[2].is_current is True


# ============================================================================
# DATA LIFECYCLE TESTS (Critical)
# ============================================================================

@pytest.mark.asyncio
async def test_delete_case_reports_preserves_runbooks(report_store, mock_redis_client, mock_vector_store):
    """Test that deleting case reports preserves runbooks (independent lifecycle)"""
    # Arrange
    mock_redis_client.zrange.return_value = [
        b"incident-report-1",
        b"runbook-1",
        b"post-mortem-1"
    ]

    # Mock metadata for each report
    mock_redis_client.hgetall.side_effect = [
        # Incident report
        {b"report_id": b"incident-report-1", b"report_type": b"incident_report"},
        # Runbook (should be preserved)
        {b"report_id": b"runbook-1", b"report_type": b"runbook"},
        # Post-mortem
        {b"report_id": b"post-mortem-1", b"report_type": b"post_mortem"},
        # Second call for incident report type check
        {b"report_id": b"incident-report-1", b"report_type": b"incident_report"},
        # Third call for post-mortem type check
        {b"report_id": b"post-mortem-1", b"report_type": b"post_mortem"}
    ]

    # Act
    result = await report_store.delete_case_reports("case-456")

    # Assert
    assert result is True

    # Verify ChromaDB delete was called with only incident_report and post_mortem
    delete_call_args = mock_vector_store.delete_documents.call_args[0][0]
    assert "incident-report-1" in delete_call_args
    assert "post-mortem-1" in delete_call_args
    assert "runbook-1" not in delete_call_args  # CRITICAL: Runbook not deleted


@pytest.mark.asyncio
async def test_delete_case_reports_only_deletes_non_runbooks(report_store, mock_redis_client):
    """Test cascade delete strategy: only incident_reports and post_mortems"""
    # Arrange
    mock_redis_client.zrange.return_value = [b"runbook-only"]
    mock_redis_client.hgetall.return_value = {
        b"report_id": b"runbook-only",
        b"report_type": b"runbook"
    }

    # Act
    result = await report_store.delete_case_reports("case-789")

    # Assert
    assert result is True
    # Pipeline delete should not be called since only runbook exists
    pipeline = mock_redis_client.pipeline_instance
    assert pipeline.delete.call_count == 0


# ============================================================================
# CASE CLOSURE TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_get_latest_reports_for_closure(report_store, mock_redis_client, mock_vector_store):
    """Test retrieving latest reports for case closure"""
    # Arrange
    mock_redis_client.hgetall.side_effect = [
        # Latest incident report
        {
            b"report_id": b"report-current",
            b"case_id": b"case-456",
            b"report_type": b"incident_report",
            b"title": b"Latest Report",
            b"format": b"markdown",
            b"generation_status": b"completed",
            b"generated_at": datetime.utcnow().isoformat().encode(),
            b"generation_time_ms": b"5000",
            b"is_current": b"true",
            b"version": b"2",
            b"linked_to_closure": b"false",
            b"metadata_json": b"null"
        }
    ]
    mock_redis_client.hget.return_value = b"report-current"

    # Act
    result = await report_store.get_latest_reports_for_closure("case-456")

    # Assert
    assert len(result) > 0
    assert all(r.is_current for r in result)


@pytest.mark.asyncio
async def test_mark_reports_linked_to_closure(report_store, mock_redis_client):
    """Test marking reports as linked to case closure"""
    # Act
    result = await report_store.mark_reports_linked_to_closure(
        "case-456",
        ["report-1", "report-2"]
    )

    # Assert
    assert result is True
    pipeline = mock_redis_client.pipeline_instance
    assert pipeline.hset.call_count >= 2  # At least 2 reports updated


# ============================================================================
# ERROR HANDLING TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_save_report_handles_redis_error(report_store, mock_redis_client):
    """Test error handling when Redis fails during save"""
    # Arrange
    mock_redis_client.hgetall.side_effect = Exception("Redis connection error")
    sample_report = CaseReport(
        report_id="report-123",
        case_id="case-456",
        report_type=ReportType.INCIDENT_REPORT,
        title="Test Report",
        content="Content",
        format="markdown",
        generation_status=ReportStatus.COMPLETED,
        generated_at=datetime.utcnow().isoformat() + 'Z',
        generation_time_ms=5000,
        is_current=True,
        version=1,
        linked_to_closure=False
    )

    # Act
    result = await report_store.save_report(sample_report)

    # Assert
    assert result is False


@pytest.mark.asyncio
async def test_get_report_handles_chromadb_error(report_store, mock_redis_client, mock_vector_store):
    """Test error handling when ChromaDB fails during retrieval"""
    # Arrange
    mock_redis_client.hgetall.return_value = {
        b"report_id": b"report-123",
        b"case_id": b"case-456",
        b"report_type": b"incident_report",
        b"title": b"Sample Report",
        b"format": b"markdown",
        b"generation_status": b"completed",
        b"generated_at": datetime.utcnow().isoformat().encode(),
        b"generation_time_ms": b"5000",
        b"is_current": b"true",
        b"version": b"1",
        b"linked_to_closure": b"false",
        b"metadata_json": b"null"
    }
    mock_vector_store.query_by_embedding.side_effect = Exception("ChromaDB error")

    # Act
    result = await report_store.get_report("report-123")

    # Assert
    # Should still return report with None content (graceful degradation)
    assert result is not None
    assert result.content is None


# ============================================================================
# CONCURRENCY TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_concurrent_report_generation_versioning(report_store, mock_redis_client):
    """Test that report versioning handles concurrent scenarios correctly

    Note: Actual concurrency control is handled by ReportLockManager.
    This test verifies the store handles version increments correctly.
    """
    # Arrange
    # Simulate version 1 exists
    mock_redis_client.hgetall.return_value = {
        b"report_id": b"report-v1",
        b"version": b"1",
        b"is_current": b"true"
    }

    new_report = CaseReport(
        report_id="report-v2",
        case_id="case-456",
        report_type=ReportType.INCIDENT_REPORT,
        title="Version 2",
        content="Updated content",
        format="markdown",
        generation_status=ReportStatus.COMPLETED,
        generated_at=datetime.utcnow().isoformat() + 'Z',
        generation_time_ms=5000,
        is_current=True,
        version=2,  # Incremented version
        linked_to_closure=False
    )

    # Act
    result = await report_store.save_report(new_report)

    # Assert
    assert result is True
    # Previous version should be marked as not current
    pipeline = mock_redis_client.pipeline_instance
    assert pipeline.hset.called
