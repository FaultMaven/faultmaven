"""Test module for Phase 2 Evidence-to-File Linkage APIs.

This module tests the new REST API endpoints that provide bidirectional linkage
between uploaded files and derived evidence:
- GET /api/v1/cases/{case_id}/uploaded-files/{file_id} - File with evidence
- GET /api/v1/cases/{case_id}/uploaded-files - List files with counts
- GET /api/v1/cases/{case_id}/evidence/{evidence_id} - Evidence with source file

Tests cover:
- File-to-evidence linkage via content_ref matching
- Evidence-to-hypothesis relationships
- Owner authorization checks
- Response model validation
- Error handling (404, 403)
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from typing import List

from fastapi.testclient import TestClient
from fastapi import status

from faultmaven.main import app
from faultmaven.models.case import (
    Case,
    CaseStatus,
    UploadedFile,
    Evidence,
    Hypothesis,
)


@pytest.fixture
def client():
    """Fixture providing FastAPI test client"""
    return TestClient(app)


@pytest.fixture
def sample_uploaded_files():
    """Sample uploaded files for testing"""
    return [
        UploadedFile(
            file_id="file_001",
            filename="application.log",
            size_bytes=5242880,  # 5MB
            data_type="log",
            uploaded_at_turn=1,
            uploaded_at=datetime(2025, 11, 11, 10, 0, 0, tzinfo=timezone.utc),
            source_type="file_upload",
            content_ref="s3://bucket/case_123/app_log.log",
            preprocessing_summary="Application log with 23 NullPointerExceptions"
        ),
        UploadedFile(
            file_id="file_002",
            filename="metrics.csv",
            size_bytes=1048576,  # 1MB
            data_type="metric",
            uploaded_at_turn=2,
            uploaded_at=datetime(2025, 11, 11, 10, 5, 0, tzinfo=timezone.utc),
            source_type="file_upload",
            content_ref="s3://bucket/case_123/metrics.csv",
            preprocessing_summary="CPU spike detected at 14:10 UTC"
        ),
        UploadedFile(
            file_id="file_003",
            filename="config.yaml",
            size_bytes=5120,  # 5KB
            data_type="config",
            uploaded_at_turn=3,
            uploaded_at=datetime(2025, 11, 11, 10, 10, 0, tzinfo=timezone.utc),
            source_type="file_upload",
            content_ref="s3://bucket/case_123/config.yaml",
            preprocessing_summary="Database config with connection pool settings"
        ),
    ]


@pytest.fixture
def sample_evidence(sample_uploaded_files):
    """Sample evidence derived from uploaded files"""
    return [
        Evidence(
            evidence_id="ev_001",
            summary="NullPointerException in auth-service after deployment",
            category="SYMPTOM_EVIDENCE",
            primary_purpose="PROBLEM_DEFINITION",
            collected_at_turn=1,
            collected_at=datetime(2025, 11, 11, 10, 1, 0, tzinfo=timezone.utc),
            collected_by="system",
            content_ref="s3://bucket/case_123/app_log.log",  # Links to file_001
            preprocessed_content="[Extracted log context around NPE]",
            content_size_bytes=5242880,
            analysis="Error started after 14:10 deployment"
        ),
        Evidence(
            evidence_id="ev_002",
            summary="CPU usage spiked to 98% at 14:10 UTC",
            category="SYMPTOM_EVIDENCE",
            primary_purpose="PROBLEM_DEFINITION",
            collected_at_turn=2,
            collected_at=datetime(2025, 11, 11, 10, 6, 0, tzinfo=timezone.utc),
            collected_by="system",
            content_ref="s3://bucket/case_123/metrics.csv",  # Links to file_002
            preprocessed_content="[Metrics analysis showing spike]",
            content_size_bytes=1048576,
            analysis="Spike correlates with error burst"
        ),
        Evidence(
            evidence_id="ev_003",
            summary="User reported auth failures starting at 14:15",
            category="SYMPTOM_EVIDENCE",
            primary_purpose="PROBLEM_DEFINITION",
            collected_at_turn=4,
            collected_at=datetime(2025, 11, 11, 10, 15, 0, tzinfo=timezone.utc),
            collected_by="user_123",
            content_ref=None,  # No source file (user-typed evidence)
            preprocessed_content="Users can't log in, getting 500 errors",
            content_size_bytes=42,
        ),
    ]


@pytest.fixture
def sample_hypotheses():
    """Sample hypotheses with evidence linkage"""
    return [
        Hypothesis(
            hypothesis_id="hyp_001",
            statement="Deployment introduced a configuration bug in auth-service",
            status="ACTIVE",
            confidence_level="MEDIUM",
            created_at=datetime(2025, 11, 11, 10, 20, 0, tzinfo=timezone.utc),
            evidence_links={
                "ev_001": "SUPPORTS",  # NPE evidence
                "ev_002": "SUPPORTS",  # CPU spike evidence
            }
        ),
        Hypothesis(
            hypothesis_id="hyp_002",
            statement="Database connection pool exhausted",
            status="PROPOSED",
            confidence_level="LOW",
            created_at=datetime(2025, 11, 11, 10, 25, 0, tzinfo=timezone.utc),
            evidence_links={
                "ev_002": "NEUTRAL",  # CPU spike could be related
            }
        ),
    ]


@pytest.fixture
def sample_case(sample_uploaded_files, sample_evidence, sample_hypotheses):
    """Complete case with files, evidence, and hypotheses"""
    return Case(
        case_id="case_123",
        title="Auth Service Down",
        owner_id="user_123",
        status=CaseStatus.INVESTIGATING,
        created_at=datetime(2025, 11, 11, 10, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2025, 11, 11, 10, 25, 0, tzinfo=timezone.utc),
        messages=[],
        uploaded_files=sample_uploaded_files,
        evidence=sample_evidence,
        hypotheses=sample_hypotheses,
    )


# ============================================================
# Test: GET /api/v1/cases/{case_id}/uploaded-files/{file_id}
# ============================================================

@pytest.mark.api
class TestGetUploadedFileDetails:
    """Tests for file details with derived evidence endpoint"""

    async def test_get_file_with_evidence_success(
        self,
        client,
        sample_case,
    ):
        """Test successful retrieval of file with derived evidence"""

        with patch('faultmaven.api.v1.routes.case.case_service') as mock_service:
            mock_service.get_case = AsyncMock(return_value=sample_case)

            response = client.get(
                f"/api/v1/cases/{sample_case.case_id}/uploaded-files/file_001",
                headers={"X-Session-ID": "session_123"}
            )

            assert response.status_code == status.HTTP_200_OK
            data = response.json()

            # Verify file metadata
            assert data["file_id"] == "file_001"
            assert data["filename"] == "application.log"
            assert data["size_bytes"] == 5242880
            assert data["size_display"] == "5.0 MB"
            assert data["uploaded_at_turn"] == 1
            assert data["data_type"] == "log"
            assert data["source_type"] == "file_upload"

            # Verify derived evidence
            assert data["evidence_count"] == 1
            assert len(data["derived_evidence"]) == 1

            evidence = data["derived_evidence"][0]
            assert evidence["evidence_id"] == "ev_001"
            assert evidence["category"] == "SYMPTOM_EVIDENCE"
            assert evidence["collected_at_turn"] == 1

            # Verify hypothesis linkage
            assert len(evidence["related_hypothesis_ids"]) == 1
            assert "hyp_001" in evidence["related_hypothesis_ids"]

    async def test_get_file_with_multiple_evidence(
        self,
        client,
        sample_case,
    ):
        """Test file with multiple derived evidence pieces"""

        # Add second evidence from same file
        second_evidence = Evidence(
            evidence_id="ev_004",
            summary="Auth service restarted 3 times",
            category="SYMPTOM_EVIDENCE",
            primary_purpose="TIMELINE_ESTABLISHMENT",
            collected_at_turn=5,
            collected_at=datetime(2025, 11, 11, 10, 30, 0, tzinfo=timezone.utc),
            collected_by="system",
            content_ref="s3://bucket/case_123/app_log.log",  # Same as file_001
            preprocessed_content="[Log entries showing restarts]",
            content_size_bytes=5242880,
        )
        sample_case.evidence.append(second_evidence)

        with patch('faultmaven.api.v1.routes.case.case_service') as mock_service:
            mock_service.get_case = AsyncMock(return_value=sample_case)

            response = client.get(
                f"/api/v1/cases/{sample_case.case_id}/uploaded-files/file_001",
                headers={"X-Session-ID": "session_123"}
            )

            assert response.status_code == status.HTTP_200_OK
            data = response.json()

            # Should show both evidence pieces
            assert data["evidence_count"] == 2
            assert len(data["derived_evidence"]) == 2

            evidence_ids = {e["evidence_id"] for e in data["derived_evidence"]}
            assert evidence_ids == {"ev_001", "ev_004"}

    async def test_get_file_no_evidence(
        self,
        client,
        sample_case,
    ):
        """Test file with no derived evidence yet"""

        with patch('faultmaven.api.v1.routes.case.case_service') as mock_service:
            mock_service.get_case = AsyncMock(return_value=sample_case)

            response = client.get(
                f"/api/v1/cases/{sample_case.case_id}/uploaded-files/file_003",
                headers={"X-Session-ID": "session_123"}
            )

            assert response.status_code == status.HTTP_200_OK
            data = response.json()

            # Config file has no derived evidence
            assert data["file_id"] == "file_003"
            assert data["evidence_count"] == 0
            assert data["derived_evidence"] == []

    async def test_get_file_not_found(
        self,
        client,
        sample_case,
    ):
        """Test 404 when file doesn't exist"""

        with patch('faultmaven.api.v1.routes.case.case_service') as mock_service:
            mock_service.get_case = AsyncMock(return_value=sample_case)

            response = client.get(
                f"/api/v1/cases/{sample_case.case_id}/uploaded-files/file_999",
                headers={"X-Session-ID": "session_123"}
            )

            assert response.status_code == status.HTTP_404_NOT_FOUND
            assert "not found" in response.json()["detail"].lower()

    async def test_get_file_wrong_owner(
        self,
        client,
        sample_case,
    ):
        """Test 403 when user doesn't own the case"""

        # Simulate different user
        sample_case.owner_id = "other_user"

        with patch('faultmaven.api.v1.routes.case.case_service') as mock_service:
            mock_service.get_case = AsyncMock(return_value=sample_case)

            response = client.get(
                f"/api/v1/cases/{sample_case.case_id}/uploaded-files/file_001",
                headers={"X-Session-ID": "session_123"}
            )

            assert response.status_code == status.HTTP_403_FORBIDDEN
            assert "access denied" in response.json()["detail"].lower()


# ============================================================
# Test: GET /api/v1/cases/{case_id}/uploaded-files
# ============================================================

@pytest.mark.api
class TestListUploadedFiles:
    """Tests for list files with evidence counts endpoint"""

    async def test_list_files_with_counts(
        self,
        client,
        sample_case,
    ):
        """Test listing all files with evidence counts"""

        with patch('faultmaven.api.v1.routes.case.case_service') as mock_service:
            mock_service.get_case = AsyncMock(return_value=sample_case)

            response = client.get(
                f"/api/v1/cases/{sample_case.case_id}/uploaded-files",
                headers={"X-Session-ID": "session_123"}
            )

            assert response.status_code == status.HTTP_200_OK
            data = response.json()

            # Verify response structure
            assert data["case_id"] == sample_case.case_id
            assert data["total_count"] == 3
            assert len(data["files"]) == 3

            # Verify files are in upload order
            files = data["files"]
            assert files[0]["file_id"] == "file_001"
            assert files[1]["file_id"] == "file_002"
            assert files[2]["file_id"] == "file_003"

            # Verify evidence counts
            assert files[0]["evidence_count"] == 1  # file_001 → ev_001
            assert files[1]["evidence_count"] == 1  # file_002 → ev_002
            assert files[2]["evidence_count"] == 0  # file_003 → no evidence

    async def test_list_files_size_formatting(
        self,
        client,
        sample_case,
    ):
        """Test file size display formatting"""

        with patch('faultmaven.api.v1.routes.case.case_service') as mock_service:
            mock_service.get_case = AsyncMock(return_value=sample_case)

            response = client.get(
                f"/api/v1/cases/{sample_case.case_id}/uploaded-files",
                headers={"X-Session-ID": "session_123"}
            )

            assert response.status_code == status.HTTP_200_OK
            files = response.json()["files"]

            # Check size formatting
            assert files[0]["size_display"] == "5.0 MB"  # 5MB
            assert files[1]["size_display"] == "1.0 MB"  # 1MB
            assert files[2]["size_display"] == "5.0 KB"  # 5KB

    async def test_list_files_empty_case(
        self,
        client,
        sample_case,
    ):
        """Test listing files when case has no uploads"""

        sample_case.uploaded_files = []

        with patch('faultmaven.api.v1.routes.case.case_service') as mock_service:
            mock_service.get_case = AsyncMock(return_value=sample_case)

            response = client.get(
                f"/api/v1/cases/{sample_case.case_id}/uploaded-files",
                headers={"X-Session-ID": "session_123"}
            )

            assert response.status_code == status.HTTP_200_OK
            data = response.json()

            assert data["total_count"] == 0
            assert data["files"] == []


# ============================================================
# Test: GET /api/v1/cases/{case_id}/evidence/{evidence_id}
# ============================================================

@pytest.mark.api
class TestGetEvidenceDetails:
    """Tests for evidence details with source file endpoint"""

    async def test_get_evidence_with_source_file(
        self,
        client,
        sample_case,
    ):
        """Test evidence derived from uploaded file"""

        with patch('faultmaven.api.v1.routes.case.case_service') as mock_service:
            mock_service.get_case = AsyncMock(return_value=sample_case)

            response = client.get(
                f"/api/v1/cases/{sample_case.case_id}/evidence/ev_001",
                headers={"X-Session-ID": "session_123"}
            )

            assert response.status_code == status.HTTP_200_OK
            data = response.json()

            # Verify evidence metadata
            assert data["evidence_id"] == "ev_001"
            assert data["case_id"] == sample_case.case_id
            assert data["category"] == "SYMPTOM_EVIDENCE"
            assert data["primary_purpose"] == "PROBLEM_DEFINITION"

            # Verify source file linkage
            assert data["source_file"] is not None
            source = data["source_file"]
            assert source["file_id"] == "file_001"
            assert source["filename"] == "application.log"
            assert source["uploaded_at_turn"] == 1

            # Verify hypothesis linkage
            assert len(data["related_hypotheses"]) == 1
            hyp = data["related_hypotheses"][0]
            assert hyp["hypothesis_id"] == "hyp_001"
            assert hyp["stance"] == "SUPPORTS"

    async def test_get_evidence_without_source_file(
        self,
        client,
        sample_case,
    ):
        """Test user-typed evidence with no source file"""

        with patch('faultmaven.api.v1.routes.case.case_service') as mock_service:
            mock_service.get_case = AsyncMock(return_value=sample_case)

            response = client.get(
                f"/api/v1/cases/{sample_case.case_id}/evidence/ev_003",
                headers={"X-Session-ID": "session_123"}
            )

            assert response.status_code == status.HTTP_200_OK
            data = response.json()

            # User-typed evidence has no source file
            assert data["evidence_id"] == "ev_003"
            assert data["source_file"] is None
            assert data["collected_by"] == "user_123"

    async def test_get_evidence_with_multiple_hypotheses(
        self,
        client,
        sample_case,
    ):
        """Test evidence linked to multiple hypotheses"""

        with patch('faultmaven.api.v1.routes.case.case_service') as mock_service:
            mock_service.get_case = AsyncMock(return_value=sample_case)

            response = client.get(
                f"/api/v1/cases/{sample_case.case_id}/evidence/ev_002",
                headers={"X-Session-ID": "session_123"}
            )

            assert response.status_code == status.HTTP_200_OK
            data = response.json()

            # ev_002 is linked to both hypotheses
            assert len(data["related_hypotheses"]) == 2

            hyp_ids = {h["hypothesis_id"] for h in data["related_hypotheses"]}
            assert hyp_ids == {"hyp_001", "hyp_002"}

            # Check stances
            stances = {h["hypothesis_id"]: h["stance"] for h in data["related_hypotheses"]}
            assert stances["hyp_001"] == "SUPPORTS"
            assert stances["hyp_002"] == "NEUTRAL"

    async def test_get_evidence_not_found(
        self,
        client,
        sample_case,
    ):
        """Test 404 when evidence doesn't exist"""

        with patch('faultmaven.api.v1.routes.case.case_service') as mock_service:
            mock_service.get_case = AsyncMock(return_value=sample_case)

            response = client.get(
                f"/api/v1/cases/{sample_case.case_id}/evidence/ev_999",
                headers={"X-Session-ID": "session_123"}
            )

            assert response.status_code == status.HTTP_404_NOT_FOUND
            assert "not found" in response.json()["detail"].lower()


# ============================================================
# Integration Tests
# ============================================================

@pytest.mark.integration
class TestEvidenceFileLinkageIntegration:
    """Integration tests for complete evidence-file workflow"""

    async def test_complete_file_upload_workflow(
        self,
        client,
        sample_case,
    ):
        """Test complete workflow: upload file → derive evidence → query linkage"""

        with patch('faultmaven.api.v1.routes.case.case_service') as mock_service:
            mock_service.get_case = AsyncMock(return_value=sample_case)

            # 1. List files - should see all uploads
            list_response = client.get(
                f"/api/v1/cases/{sample_case.case_id}/uploaded-files",
                headers={"X-Session-ID": "session_123"}
            )
            assert list_response.status_code == status.HTTP_200_OK
            assert list_response.json()["total_count"] == 3

            # 2. Get file details - should show derived evidence
            file_response = client.get(
                f"/api/v1/cases/{sample_case.case_id}/uploaded-files/file_001",
                headers={"X-Session-ID": "session_123"}
            )
            assert file_response.status_code == status.HTTP_200_OK
            assert file_response.json()["evidence_count"] == 1

            # 3. Get evidence details - should link back to source file
            evidence_response = client.get(
                f"/api/v1/cases/{sample_case.case_id}/evidence/ev_001",
                headers={"X-Session-ID": "session_123"}
            )
            assert evidence_response.status_code == status.HTTP_200_OK
            evidence_data = evidence_response.json()
            assert evidence_data["source_file"]["file_id"] == "file_001"

    async def test_bidirectional_linkage_consistency(
        self,
        client,
        sample_case,
    ):
        """Test bidirectional linkage is consistent"""

        with patch('faultmaven.api.v1.routes.case.case_service') as mock_service:
            mock_service.get_case = AsyncMock(return_value=sample_case)

            # Get file → evidence
            file_resp = client.get(
                f"/api/v1/cases/{sample_case.case_id}/uploaded-files/file_001",
                headers={"X-Session-ID": "session_123"}
            )
            derived_evidence_ids = {
                e["evidence_id"] for e in file_resp.json()["derived_evidence"]
            }

            # For each derived evidence, verify it links back to this file
            for evidence_id in derived_evidence_ids:
                ev_resp = client.get(
                    f"/api/v1/cases/{sample_case.case_id}/evidence/{evidence_id}",
                    headers={"X-Session-ID": "session_123"}
                )
                source_file = ev_resp.json()["source_file"]
                assert source_file is not None
                assert source_file["file_id"] == "file_001"
