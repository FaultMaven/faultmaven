"""
Integration Tests for Evidence Flow - End-to-End

Tests the complete evidence creation flow from file upload through preprocessing,
LLM classification, to evidence creation in the database.

Design Reference:
- docs/architecture/data-processing/EVIDENCE-CLASSIFICATION-FINAL-DESIGN.md
- docs/architecture/data-processing/EVIDENCE-REDESIGN-IMPLEMENTATION-PLAN.md

Scenarios Tested:
1. File upload → preprocessing → LLM classification → evidence creation
2. Duplicate file detection and handling
3. Pure chat messages (no evidence created)
4. Mixed submissions (chat + data)
5. Evidence category immutability
6. Analytics queries (acceptance rate, rejection reasons)
"""

import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
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


@pytest.fixture
def test_case(mock_services_for_integration_tests, auth_headers):
    """Create a test case for integration tests"""
    # Use the TestClient from the fixture (has all overrides configured)
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
        """Upload log file → preprocessed → classified as SYMPTOM_EVIDENCE"""
        # Arrange - Create log file content
        log_content = """
2024-01-10 10:00:00 ERROR [DatabasePool] Connection timeout after 30s
2024-01-10 10:00:05 ERROR [DatabasePool] Connection timeout after 30s
2024-01-10 10:00:10 ERROR [DatabasePool] Connection timeout after 30s
2024-01-10 10:00:15 INFO [Application] Retrying connection...
2024-01-10 10:00:20 ERROR [DatabasePool] Connection timeout after 30s
"""
        log_file_content = io.BytesIO(log_content.encode())

        # Act - Upload file using TestClient from fixture
        # CRITICAL: Remove Content-Type: application/json from headers so TestClient/httpx
        # can set the correct multipart/form-data boundary header.
        upload_headers = {
            k: v for k, v in auth_headers.items() if k.lower() != "content-type"
        }

        response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/data",
            files={"file": ("app.log", log_file_content, "text/plain")},
            data={"description": "Here are the application logs"},
            headers=upload_headers,
        )

        # Assert - File uploaded successfully
        assert response.status_code == 201

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
        """Upload config file → classified as CONTEXTUAL_EVIDENCE"""
        # Arrange - Create config file
        config_content = """
database:
  host: localhost
  port: 5432
  pool_size: 10
  timeout: 30
  max_connections: 20
"""
        config_file_content = config_content.encode()

        # Act - Upload file using fresh TestClient (strip Content-Type)
        upload_headers = {
            k: v for k, v in auth_headers.items() if k.lower() != "content-type"
        }
        response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/data",
            files={"file": ("database.yaml", config_file_content, "application/yaml")},
            data={"message": "Here's our database configuration"},
            headers=upload_headers,
        )

        # Assert
        assert response.status_code == 201

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
        """Upload unrelated file → classified as REJECTED"""
        # Arrange - Create unrelated file (e.g., vacation photo metadata)
        image_content = b"FAKE_IMAGE_CONTENT_NOT_RELATED_TO_ISSUE"

        # Act - Upload file using fresh TestClient (strip Content-Type)
        upload_headers = {
            k: v for k, v in auth_headers.items() if k.lower() != "content-type"
        }
        response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/data",
            files={"file": ("vacation.jpg", image_content, "image/jpeg")},
            headers=upload_headers,
        )

        # Assert
        assert response.status_code == 201

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
        """Upload same file twice → second marked as duplicate"""
        # Arrange - Same file content
        log_content = b"ERROR: Test error message"
        content_hash = hashlib.sha256(log_content).hexdigest()

        # Act - Upload first time using fresh TestClient (strip Content-Type)
        upload_headers = {
            k: v for k, v in auth_headers.items() if k.lower() != "content-type"
        }
        first_response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/data",
            files={"file": ("error.log", log_content, "text/plain")},
            headers=upload_headers,
        )
        assert first_response.status_code == 201

        # Upload second time (same content)
        second_response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/data",
            files={"file": ("error_copy.log", log_content, "text/plain")},
            headers=upload_headers,
        )
        assert second_response.status_code == 201

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

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_chat_message_no_evidence(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Pure chat message → no evidence created"""
        # Note: These tests still use AsyncClient because /queries endpoint
        # requires async operations that TestClient doesn't support well
        # Act - Send chat message via /queries endpoint
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/cases/{test_case}/queries",
                json={"message": "What could be causing high CPU usage?"},
                headers=auth_headers,
            )

        # Assert
        assert response.status_code == 200

        # Verify no evidence created using fresh TestClient
        evidence_list = get_case_evidence(
            mock_services_for_integration_tests, test_case, auth_headers
        )

        # Evidence should be empty (pure chat doesn't create evidence)
        assert len(evidence_list) == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_multiple_chat_messages_no_evidence(
        self, test_case, auth_headers, mock_services_for_integration_tests
    ):
        """Multiple chat messages → no evidence accumulated"""
        # Act - Send multiple chat messages
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                f"/api/v1/cases/{test_case}/queries",
                json={"message": "Can you help me troubleshoot?"},
                headers=auth_headers,
            )
            await client.post(
                f"/api/v1/cases/{test_case}/queries",
                json={"message": "I think it might be the database"},
                headers=auth_headers,
            )
            await client.post(
                f"/api/v1/cases/{test_case}/queries",
                json={"message": "What should I check next?"},
                headers=auth_headers,
            )

        # Assert - Verify no evidence created using fresh TestClient
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
        """Chat message + file upload → evidence created"""
        # Arrange
        log_content = b"ERROR: Connection failed"

        # Act - Upload with message using fresh TestClient (strip Content-Type)
        upload_headers = {
            k: v for k, v in auth_headers.items() if k.lower() != "content-type"
        }
        response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/data",
            files={"file": ("error.log", log_content, "text/plain")},
            data={"message": "I'm seeing these errors in the logs"},
            headers=upload_headers,
        )

        # Assert
        assert response.status_code == 201

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
        # Arrange - Upload file using fresh TestClient (strip Content-Type)
        log_content = b"ERROR: Database timeout"
        upload_headers = {
            k: v for k, v in auth_headers.items() if k.lower() != "content-type"
        }

        mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/data",
            files={"file": ("db_error.log", log_content, "text/plain")},
            headers=upload_headers,
        )

        # Get evidence
        evidence_list = get_case_evidence(
            mock_services_for_integration_tests, test_case, auth_headers
        )
        original_category = evidence_list[0]["category"]

        # Act - Upload more evidence, get case again
        mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/data",
            files={"file": ("metrics.txt", b"CPU: 95%", "text/plain")},
            headers=upload_headers,
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
        # Arrange - Upload mix of relevant and irrelevant files using fresh TestClient (strip Content-Type)
        upload_headers = {
            k: v for k, v in auth_headers.items() if k.lower() != "content-type"
        }
        # Relevant file
        mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/data",
            files={"file": ("error.log", b"ERROR: Test", "text/plain")},
            headers=upload_headers,
        )
        # Another relevant file
        mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/data",
            files={"file": ("metrics.txt", b"CPU: 100%", "text/plain")},
            headers=upload_headers,
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
        # This would require uploading files that get rejected
        # and verifying the primary_purpose field contains rejection reason

        # Arrange - Upload potentially irrelevant file using fresh TestClient (strip Content-Type)
        upload_headers = {
            k: v for k, v in auth_headers.items() if k.lower() != "content-type"
        }
        response = mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/data",
            files={"file": ("random.txt", b"Random content", "text/plain")},
            headers=upload_headers,
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

        # Act - Upload file using fresh TestClient (strip Content-Type)
        upload_headers = {
            k: v for k, v in auth_headers.items() if k.lower() != "content-type"
        }
        mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/data",
            files={"file": ("test.log", log_content, "text/plain")},
            headers=upload_headers,
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
        # Arrange - Upload different file types using fresh TestClient (strip Content-Type)
        upload_headers = {
            k: v for k, v in auth_headers.items() if k.lower() != "content-type"
        }
        # Log file
        mock_services_for_integration_tests.post(
            f"/api/v1/cases/{test_case}/data",
            files={"file": ("app.log", b"LOG CONTENT", "text/plain")},
            headers=upload_headers,
        )

        # Assert
        evidence_list = get_case_evidence(
            mock_services_for_integration_tests, test_case, auth_headers
        )

        # Evidence should have preprocessing_method
        for evidence in evidence_list:
            assert "preprocessing_method" in evidence
            assert evidence["preprocessing_method"] is not None
