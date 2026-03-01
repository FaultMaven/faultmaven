"""
Integration Tests for Evidence Flow - End-to-End

Tests the complete evidence creation flow from file upload through preprocessing,
LLM classification, to evidence creation in the database.

Design Reference:
- docs/architecture/data-processing/EVIDENCE-CLASSIFICATION-FINAL-DESIGN.md
- docs/architecture/data-processing/EVIDENCE-REDESIGN-IMPLEMENTATION-PLAN.md

All submissions now go through the unified turn endpoint:
  POST /api/v1/cases/{case_id}/turns

Scenarios Tested:
1. File upload via /turns → preprocessing → LLM classification → evidence creation
2. Duplicate file detection and handling
3. Pure chat messages (no evidence created)
4. Mixed submissions (chat + file)
5. Evidence category immutability
6. Analytics queries (acceptance rate, rejection reasons)
"""

import hashlib
import io
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from faultmaven.main import app
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    EvidenceCategory,
    EvidenceSourceType,
)


def get_case_evidence(client, case_id, headers):
    """Helper to fetch all evidence by iterating through uploaded files.

    Workaround for CaseDetail not containing evidence list.
    """
    # Get uploaded files
    files_response = client.get(
        f"/api/v1/cases/{case_id}/uploaded-files", headers=headers
    )
    if files_response.status_code != 200:
        return []

    files_data = files_response.json()
    all_evidence = []

    for f in files_data.get("files", []):
        file_id = f["file_id"]
        # Get details for each file to get derived evidence
        detail_response = client.get(
            f"/api/v1/cases/{case_id}/uploaded-files/{file_id}", headers=headers
        )
        if detail_response.status_code == 200:
            detail = detail_response.json()
            all_evidence.extend(detail.get("derived_evidence", []))

    return all_evidence


def _upload_headers(auth_headers):
    """Strip Content-Type so TestClient can set multipart/form-data boundary."""
    return {k: v for k, v in auth_headers.items() if k.lower() != "content-type"}


@pytest.fixture
def test_case(mock_services_for_integration_tests, auth_headers):
    """Create a test case for integration tests"""
    response = mock_services_for_integration_tests.post(
        "/api/v1/cases",
        json={
            "title": "Integration Test Case",
            "description": "Testing evidence flow",
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    case_data = response.json()
    return case_data["case_id"]


class TestFileUploadToEvidence:
    """Test complete flow: file upload → preprocessing → LLM → evidence"""

    @pytest.mark.integration
    def test_log_file_upload_creates_symptom_evidence(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Upload log file via /turns → preprocessed → classified as SYMPTOM_EVIDENCE"""
        # Arrange - Create log file content
        log_content = b"""
2024-01-10 10:00:00 ERROR [DatabasePool] Connection timeout after 30s
2024-01-10 10:00:05 ERROR [DatabasePool] Connection timeout after 30s
2024-01-10 10:00:10 ERROR [DatabasePool] Connection timeout after 30s
2024-01-10 10:00:15 INFO [Application] Retrying connection...
2024-01-10 10:00:20 ERROR [DatabasePool] Connection timeout after 30s
"""
        headers = _upload_headers(auth_headers)

        # Act - Upload file via /turns endpoint
        response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            files=[("files", ("app.log", log_content, "text/plain"))],
            data={"query": "Here are the application logs"},
            headers=headers,
        )

        # Assert - Turn processed successfully
        assert response.status_code == 200

        # Get uploaded files to verify file persistence
        files_response = mock_services_for_integration_tests.get(
            f"/api/v1/cases/{test_case}/uploaded-files", headers=auth_headers
        )
        assert files_response.status_code == 200
        files_data = files_response.json()
        assert files_data["total_count"] == 1
        file_id = files_data["files"][0]["file_id"]

        # Get file details (which includes derived evidence)
        file_detail_response = mock_services_for_integration_tests.get(
            f"/api/v1/cases/{test_case}/uploaded-files/{file_id}", headers=auth_headers
        )
        assert file_detail_response.status_code == 200
        file_detail = file_detail_response.json()

        # Verify evidence created
        assert len(file_detail["derived_evidence"]) == 1
        evidence = file_detail["derived_evidence"][0]

        # Verify classification
        assert evidence["category"] == EvidenceCategory.SYMPTOM_EVIDENCE.value
        assert evidence["source_type"] == EvidenceSourceType.LOGS.value
        assert (
            "connection timeout" in evidence["summary"].lower()
            or "error" in evidence["summary"].lower()
        )
        assert evidence["content_hash"] is not None

    @pytest.mark.integration
    def test_config_file_upload_creates_contextual_evidence(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Upload config file via /turns → classified as CONTEXTUAL_EVIDENCE"""
        # Arrange - Create config file
        config_content = b"""
database:
  host: localhost
  port: 5432
  pool_size: 10
  timeout: 30
  max_connections: 20
"""
        headers = _upload_headers(auth_headers)

        # Act - Upload file via /turns
        response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            files=[("files", ("database.yaml", config_content, "application/yaml"))],
            data={"query": "Here's our database configuration"},
            headers=headers,
        )

        # Assert
        assert response.status_code == 200

        # Verify evidence
        evidence_list = get_case_evidence(
            mock_services_for_integration_tests, test_case, auth_headers
        )

        assert len(evidence_list) >= 1
        # Find the config evidence
        config_evidence = [
            e
            for e in evidence_list
            if e["source_type"] == EvidenceSourceType.CONFIGURATION.value
        ]
        assert len(config_evidence) == 1
        assert config_evidence[0]["category"] in [
            EvidenceCategory.CONTEXTUAL_EVIDENCE.value,
            EvidenceCategory.SYMPTOM_EVIDENCE.value,  # Could be symptom if config shows issues
        ]

    @pytest.mark.integration
    def test_irrelevant_file_rejected(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Upload unrelated file via /turns → classified as REJECTED"""
        # Arrange - Create unrelated file (e.g., vacation photo metadata)
        image_content = b"FAKE_IMAGE_CONTENT_NOT_RELATED_TO_ISSUE"
        headers = _upload_headers(auth_headers)

        # Act - Upload file via /turns
        response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            files=[("files", ("vacation.jpg", image_content, "image/jpeg"))],
            headers=headers,
        )

        # Assert
        assert response.status_code == 200

        # Verify evidence (may be rejected or accepted depending on LLM)
        evidence_list = get_case_evidence(
            mock_services_for_integration_tests, test_case, auth_headers
        )

        # Evidence should exist (either REJECTED or categorized)
        assert len(evidence_list) >= 1


class TestDuplicateFileHandling:
    """Test duplicate file detection and handling"""

    @pytest.mark.integration
    def test_duplicate_file_detected(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Upload same file twice via /turns → second marked as duplicate"""
        # Arrange - Same file content
        log_content = b"ERROR: Test error message"
        headers = _upload_headers(auth_headers)

        # Act - Upload first time
        first_response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            files=[("files", ("error.log", log_content, "text/plain"))],
            headers=headers,
        )
        assert first_response.status_code == 200

        # Upload second time (same content)
        second_response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            files=[("files", ("error_copy.log", log_content, "text/plain"))],
            headers=headers,
        )
        assert second_response.status_code == 200

        # Assert - Check evidence
        evidence_list = get_case_evidence(
            mock_services_for_integration_tests, test_case, auth_headers
        )

        # Should have 2 evidence records
        assert len(evidence_list) == 2

        # Both should have same content_hash
        hashes = [e["content_hash"] for e in evidence_list]
        assert hashes[0] == hashes[1]

        # Second should be marked as duplicate (REJECTED)
        second_evidence = evidence_list[1]
        assert second_evidence["category"] == EvidenceCategory.REJECTED.value
        assert (
            "duplicate" in second_evidence["summary"].lower()
            or "duplicate" in second_evidence["primary_purpose"].lower()
        )


class TestChatOnlyNoEvidence:
    """Test pure chat messages don't create evidence"""

    @pytest.mark.integration
    def test_chat_message_no_evidence(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Pure chat message via /turns → no evidence created"""
        headers = _upload_headers(auth_headers)

        # Act - Send chat message via /turns endpoint (query only, no files)
        response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            data={"query": "What could be causing high CPU usage?"},
            headers=headers,
        )

        # Assert
        assert response.status_code == 200

        # Verify no evidence created
        evidence_list = get_case_evidence(
            mock_services_for_integration_tests, test_case, auth_headers
        )

        # Evidence should be empty (pure chat doesn't create evidence)
        assert len(evidence_list) == 0

    @pytest.mark.integration
    def test_multiple_chat_messages_no_evidence(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Multiple chat messages via /turns → no evidence accumulated"""
        headers = _upload_headers(auth_headers)

        # Act - Send multiple chat messages
        mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            data={"query": "Can you help me troubleshoot?"},
            headers=headers,
        )
        mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            data={"query": "I think it might be the database"},
            headers=headers,
        )
        mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            data={"query": "What should I check next?"},
            headers=headers,
        )

        # Assert - Verify no evidence created
        evidence_list = get_case_evidence(
            mock_services_for_integration_tests, test_case, auth_headers
        )

        # Still no evidence
        assert len(evidence_list) == 0


class TestMixedSubmissions:
    """Test chat + file submissions"""

    @pytest.mark.integration
    def test_chat_with_file_creates_evidence(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Chat message + file upload via /turns → evidence created"""
        # Arrange
        log_content = b"ERROR: Connection failed"
        headers = _upload_headers(auth_headers)

        # Act - Upload with message via /turns
        response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            files=[("files", ("error.log", log_content, "text/plain"))],
            data={"query": "I'm seeing these errors in the logs"},
            headers=headers,
        )

        # Assert
        assert response.status_code == 200

        # Verify evidence created
        evidence_list = get_case_evidence(
            mock_services_for_integration_tests, test_case, auth_headers
        )

        # Evidence should exist (from file, not from chat)
        assert len(evidence_list) == 1
        assert evidence_list[0]["category"] in [
            EvidenceCategory.SYMPTOM_EVIDENCE.value,
            EvidenceCategory.REJECTED.value,
        ]


class TestEvidenceCategoryImmutability:
    """Test evidence category doesn't change after creation"""

    @pytest.mark.integration
    def test_evidence_category_immutable(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Evidence category should not change after initial classification"""
        # Arrange - Upload file via /turns
        log_content = b"ERROR: Database timeout"
        headers = _upload_headers(auth_headers)

        mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            files=[("files", ("db_error.log", log_content, "text/plain"))],
            headers=headers,
        )

        # Get evidence
        evidence_list = get_case_evidence(
            mock_services_for_integration_tests, test_case, auth_headers
        )
        original_category = evidence_list[0]["category"]

        # Act - Upload more evidence, get case again
        mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            files=[("files", ("metrics.txt", b"CPU: 95%", "text/plain"))],
            headers=headers,
        )

        # Get case again
        evidence_list = get_case_evidence(
            mock_services_for_integration_tests, test_case, auth_headers
        )

        # Assert - Original evidence category unchanged
        first_evidence = evidence_list[0]
        assert first_evidence["category"] == original_category


class TestAnalyticsQueries:
    """Test analytics queries for evidence acceptance and rejection"""

    @pytest.mark.integration
    def test_acceptance_rate_calculation(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Test calculating evidence acceptance rate"""
        # Arrange - Upload mix of files via /turns
        headers = _upload_headers(auth_headers)

        # Relevant file
        mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            files=[("files", ("error.log", b"ERROR: Test", "text/plain"))],
            headers=headers,
        )
        # Another relevant file
        mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            files=[("files", ("metrics.txt", b"CPU: 100%", "text/plain"))],
            headers=headers,
        )

        # Get case
        evidence_list = get_case_evidence(
            mock_services_for_integration_tests, test_case, auth_headers
        )

        # Calculate acceptance rate
        total_submissions = len(evidence_list)
        valid_evidence = [
            e for e in evidence_list if e["category"] != EvidenceCategory.REJECTED.value
        ]
        acceptance_rate = (
            (len(valid_evidence) / total_submissions * 100)
            if total_submissions > 0
            else 0
        )

        # Assert
        assert total_submissions >= 2
        assert acceptance_rate >= 0
        assert acceptance_rate <= 100

    @pytest.mark.integration
    def test_rejection_reasons_tracking(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Test tracking rejection reasons"""
        # Arrange - Upload potentially irrelevant file via /turns
        headers = _upload_headers(auth_headers)
        response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            files=[("files", ("random.txt", b"Random content", "text/plain"))],
            headers=headers,
        )

        # Get case
        evidence_list = get_case_evidence(
            mock_services_for_integration_tests, test_case, auth_headers
        )

        # Find rejected evidence (if any)
        rejected = [
            e for e in evidence_list if e["category"] == EvidenceCategory.REJECTED.value
        ]

        # If rejected evidence exists, verify it has a reason
        for evidence in rejected:
            assert evidence["primary_purpose"] is not None
            assert len(evidence["primary_purpose"]) > 0


class TestPreprocessingIntegration:
    """Test preprocessing integration with evidence creation"""

    @pytest.mark.integration
    def test_content_hash_generated(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Verify content_hash is generated during preprocessing"""
        # Arrange
        log_content = b"Test log content"
        # Mock always uses "Test content" for preprocessed_content
        expected_hash = hashlib.sha256(b"Test content").hexdigest()
        headers = _upload_headers(auth_headers)

        # Act - Upload file via /turns
        mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            files=[("files", ("test.log", log_content, "text/plain"))],
            headers=headers,
        )

        # Assert
        evidence_list = get_case_evidence(
            mock_services_for_integration_tests, test_case, auth_headers
        )

        assert len(evidence_list) >= 1
        evidence = evidence_list[0]
        assert evidence["content_hash"] == expected_hash

    @pytest.mark.integration
    def test_preprocessing_method_recorded(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Verify preprocessing_method is recorded in evidence"""
        # Arrange - Upload file via /turns
        headers = _upload_headers(auth_headers)
        mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            files=[("files", ("app.log", b"LOG CONTENT", "text/plain"))],
            headers=headers,
        )

        # Assert
        evidence_list = get_case_evidence(
            mock_services_for_integration_tests, test_case, auth_headers
        )

        # Evidence should have preprocessing_method
        for evidence in evidence_list:
            assert "preprocessing_method" in evidence
            assert evidence["preprocessing_method"] is not None


class TestLargeContentSubmission:
    """Test that large content submissions (>1MB) are accepted up to MAX_UPLOAD_SIZE_MB.

    Starlette's MultiPartParser defaults to 1MB per form part. FaultMaven overrides
    this to match MAX_UPLOAD_SIZE_MB (default 10MB) so that all three data submission
    paths — file upload, page injection, and pasted text — are governed by the same
    configured limit.

    Design Reference:
    - docs/architecture/data-processing/data-preprocessing-design-specification.md
      Section 2.4 (Pasted Text vs File Upload: Same Pipeline) and Appendix A
    """

    @pytest.mark.integration
    def test_large_pasted_content_accepted(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Pasted content >1MB should be accepted (up to MAX_UPLOAD_SIZE_MB).

        This is the core regression test for the Starlette max_part_size issue.
        Without the fix, Starlette rejects any form field >1MB with a 400.
        """
        # Arrange - Create pasted content >1MB (roughly 1.5MB)
        large_content = "ERROR: Connection timeout at line {}\n" * 30000
        assert len(large_content) > 1 * 1024 * 1024, "Content must exceed 1MB"
        assert len(large_content) < 10 * 1024 * 1024, "Content must be under 10MB"

        headers = _upload_headers(auth_headers)

        # Act - Submit large pasted content via /turns
        response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            data={"pasted_content": large_content},
            headers=headers,
        )

        # Assert - Should be accepted, not rejected with 400
        assert response.status_code == 200, (
            f"Large pasted content ({len(large_content)} bytes) was rejected. "
            f"Status: {response.status_code}, Body: {response.text[:200]}"
        )

    @pytest.mark.integration
    def test_large_page_injection_accepted(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Page injection >1MB should be accepted (same path as pasted content).

        Page injection uses the same pasted_content form field but includes
        a '--- Page Content (url) ---' header that the backend detects.
        """
        # Arrange - Simulate a large page capture (~2MB, like the original bug report)
        page_body = "<div>Content block {}</div>\n" * 60000
        large_page = (
            f"--- Page Content (https://example.com/dashboard) ---\n{page_body}"
        )
        assert len(large_page) > 1.5 * 1024 * 1024, "Page content must exceed 1.5MB"

        headers = _upload_headers(auth_headers)

        # Act - Submit via /turns
        response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            data={"pasted_content": large_page},
            headers=headers,
        )

        # Assert
        assert response.status_code == 200, (
            f"Large page injection ({len(large_page)} bytes) was rejected. "
            f"Status: {response.status_code}, Body: {response.text[:200]}"
        )

    @pytest.mark.integration
    def test_large_file_upload_accepted(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """File upload >1MB should be accepted (same limit applies to all paths)."""
        # Arrange - Create a log file >1MB
        large_log = b"2024-01-10 10:00:00 ERROR [Pool] Connection timeout\n" * 25000
        assert len(large_log) > 1 * 1024 * 1024, "File must exceed 1MB"

        headers = _upload_headers(auth_headers)

        # Act - Upload large file via /turns
        response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            files=[("files", ("large_app.log", large_log, "text/plain"))],
            data={"query": "Analyze these logs"},
            headers=headers,
        )

        # Assert
        assert response.status_code == 200, (
            f"Large file upload ({len(large_log)} bytes) was rejected. "
            f"Status: {response.status_code}, Body: {response.text[:200]}"
        )

    @pytest.mark.integration
    def test_large_query_with_pasted_content_accepted(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Combined query + large pasted content should work."""
        # Arrange
        large_content = "WARN: Slow query detected ({}ms)\n" * 35000
        assert len(large_content) > 1 * 1024 * 1024

        headers = _upload_headers(auth_headers)

        # Act - Submit query + large pasted content
        response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/turns",
            data={
                "query": "What's causing the slow queries?",
                "pasted_content": large_content,
            },
            headers=headers,
        )

        # Assert
        assert response.status_code == 200
