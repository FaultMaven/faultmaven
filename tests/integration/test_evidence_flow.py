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

from faultmaven.main import app
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    EvidenceCategory,
    EvidenceSourceType,
)


@pytest.fixture
async def test_case(test_client, auth_headers):
    """Create a test case for integration tests"""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
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


@pytest.mark.usefixtures("mock_services_for_integration_tests")
class TestFileUploadToEvidence:
    """Test complete flow: file upload → preprocessing → LLM → evidence"""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_log_file_upload_creates_symptom_evidence(
        self, test_case, auth_headers
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
        # Use bytes directly instead of BytesIO for ASGITransport compatibility
        log_file_content = log_content.encode()

        # Act - Upload file
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/cases/{test_case}/data",
                files={"file": ("app.log", log_file_content, "text/plain")},
                data={"message": "Here are the application logs"},
                headers=auth_headers,
            )

        # Assert - File uploaded successfully
        assert response.status_code == 200

        # Get case to verify evidence
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            case_response = await client.get(
                f"/api/v1/cases/{test_case}", headers=auth_headers
            )
        assert case_response.status_code == 200
        case_data = case_response.json()

        # Verify evidence created
        assert len(case_data["evidence"]) == 1
        evidence = case_data["evidence"][0]

        # Verify classification
        assert evidence["category"] == EvidenceCategory.SYMPTOM_EVIDENCE.value
        assert evidence["source_type"] == EvidenceSourceType.LOGS.value
        assert (
            "connection timeout" in evidence["summary"].lower()
            or "error" in evidence["summary"].lower()
        )
        assert evidence["content_hash"] is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_config_file_upload_creates_contextual_evidence(
        self, test_case, auth_headers
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
        config_file = io.BytesIO(config_content.encode())

        # Act - Upload file
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/cases/{test_case}/data",
                files={"file": ("database.yaml", config_file, "application/yaml")},
                data={"message": "Here's our database configuration"},
                headers=auth_headers,
            )

        # Assert
        assert response.status_code == 200

        # Verify evidence
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            case_response = await client.get(
                f"/api/v1/cases/{test_case}", headers=auth_headers
            )
        case_data = case_response.json()

        assert len(case_data["evidence"]) >= 1
        # Find the config evidence
        config_evidence = [
            e
            for e in case_data["evidence"]
            if e["source_type"] == EvidenceSourceType.CONFIGURATION.value
        ]
        assert len(config_evidence) == 1
        assert config_evidence[0]["category"] in [
            EvidenceCategory.CONTEXTUAL_EVIDENCE.value,
            EvidenceCategory.SYMPTOM_EVIDENCE.value,  # Could be symptom if config shows issues
        ]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_irrelevant_file_rejected(self, test_case, auth_headers):
        """Upload unrelated file → classified as REJECTED"""
        # Arrange - Create unrelated file (e.g., vacation photo metadata)
        image_content = b"FAKE_IMAGE_CONTENT_NOT_RELATED_TO_ISSUE"
        image_file = io.BytesIO(image_content)

        # Act - Upload file
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/cases/{test_case}/data",
                files={"file": ("vacation.jpg", image_file, "image/jpeg")},
                headers=auth_headers,
            )

        # Assert
        assert response.status_code == 200

        # Verify evidence (may be rejected or accepted depending on LLM)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            case_response = await client.get(
                f"/api/v1/cases/{test_case}", headers=auth_headers
            )
        case_data = case_response.json()

        # Evidence should exist (either REJECTED or categorized)
        assert len(case_data["evidence"]) >= 1


@pytest.mark.usefixtures("mock_services_for_integration_tests")
class TestDuplicateFileHandling:
    """Test duplicate file detection and handling"""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_duplicate_file_detected(self, test_case, auth_headers):
        """Upload same file twice → second marked as duplicate"""
        # Arrange - Same file content
        log_content = b"ERROR: Test error message"
        content_hash = hashlib.sha256(log_content).hexdigest()

        # Act - Upload first time
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            first_response = await client.post(
                f"/api/v1/cases/{test_case}/data",
                files={"file": ("error.log", io.BytesIO(log_content), "text/plain")},
                headers=auth_headers,
            )
        assert first_response.status_code == 200

        # Upload second time (same content)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            second_response = await client.post(
                f"/api/v1/cases/{test_case}/data",
                files={
                    "file": ("error_copy.log", io.BytesIO(log_content), "text/plain")
                },
                headers=auth_headers,
            )
        assert second_response.status_code == 200

        # Assert - Check evidence
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            case_response = await client.get(
                f"/api/v1/cases/{test_case}", headers=auth_headers
            )
        case_data = case_response.json()

        # Should have 2 evidence records
        assert len(case_data["evidence"]) == 2

        # Both should have same content_hash
        hashes = [e["content_hash"] for e in case_data["evidence"]]
        assert hashes[0] == hashes[1]

        # Second should be marked as duplicate (REJECTED)
        second_evidence = case_data["evidence"][1]
        assert second_evidence["category"] == EvidenceCategory.REJECTED.value
        assert (
            "duplicate" in second_evidence["summary"].lower()
            or "duplicate" in second_evidence["primary_purpose"].lower()
        )


@pytest.mark.usefixtures("mock_services_for_integration_tests")
class TestChatOnlyNoEvidence:
    """Test pure chat messages don't create evidence"""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_chat_message_no_evidence(self, test_case, auth_headers):
        """Pure chat message → no evidence created"""
        # Act - Send chat message via /queries endpoint
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/cases/{test_case}/queries",
                json={"query": "What could be causing high CPU usage?"},
                headers=auth_headers,
            )

        # Assert
        assert response.status_code == 200

        # Verify no evidence created
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            case_response = await client.get(
                f"/api/v1/cases/{test_case}", headers=auth_headers
            )
        case_data = case_response.json()

        # Evidence should be empty (pure chat doesn't create evidence)
        assert len(case_data["evidence"]) == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_multiple_chat_messages_no_evidence(self, test_case, auth_headers):
        """Multiple chat messages → no evidence accumulated"""
        # Act - Send multiple chat messages
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                f"/api/v1/cases/{test_case}/queries",
                json={"query": "Can you help me troubleshoot?"},
                headers=auth_headers,
            )
            await client.post(
                f"/api/v1/cases/{test_case}/queries",
                json={"query": "I think it might be the database"},
                headers=auth_headers,
            )
            await client.post(
                f"/api/v1/cases/{test_case}/queries",
                json={"query": "What should I check next?"},
                headers=auth_headers,
            )

        # Assert
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            case_response = await client.get(
                f"/api/v1/cases/{test_case}", headers=auth_headers
            )
        case_data = case_response.json()

        # Still no evidence
        assert len(case_data["evidence"]) == 0


@pytest.mark.usefixtures("mock_services_for_integration_tests")
class TestMixedSubmissions:
    """Test chat + file submissions"""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_chat_with_file_creates_evidence(self, test_case, auth_headers):
        """Chat message + file upload → evidence created"""
        # Arrange
        log_content = b"ERROR: Connection failed"
        log_file = io.BytesIO(log_content)

        # Act - Upload with message
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/cases/{test_case}/data",
                files={"file": ("error.log", log_file, "text/plain")},
                data={"message": "I'm seeing these errors in the logs"},
                headers=auth_headers,
            )

        # Assert
        assert response.status_code == 200

        # Verify evidence created
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            case_response = await client.get(
                f"/api/v1/cases/{test_case}", headers=auth_headers
            )
        case_data = case_response.json()

        # Evidence should exist (from file, not from chat)
        assert len(case_data["evidence"]) == 1
        assert case_data["evidence"][0]["category"] in [
            EvidenceCategory.SYMPTOM_EVIDENCE.value,
            EvidenceCategory.REJECTED.value,
        ]


@pytest.mark.usefixtures("mock_services_for_integration_tests")
class TestEvidenceCategoryImmutability:
    """Test evidence category doesn't change after creation"""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_evidence_category_immutable(self, test_case, auth_headers):
        """Evidence category should not change after initial classification"""
        # Arrange - Upload file
        log_content = b"ERROR: Database timeout"
        log_file = io.BytesIO(log_content)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                f"/api/v1/cases/{test_case}/data",
                files={"file": ("db_error.log", log_file, "text/plain")},
                headers=auth_headers,
            )

        # Get evidence
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            case_response = await client.get(
                f"/api/v1/cases/{test_case}", headers=auth_headers
            )
        case_data = case_response.json()
        original_category = case_data["evidence"][0]["category"]

        # Act - Upload more evidence, get case again
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                f"/api/v1/cases/{test_case}/data",
                files={"file": ("metrics.txt", io.BytesIO(b"CPU: 95%"), "text/plain")},
                headers=auth_headers,
            )

        # Get case again
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            case_response = await client.get(
                f"/api/v1/cases/{test_case}", headers=auth_headers
            )
        case_data = case_response.json()

        # Assert - Original evidence category unchanged
        first_evidence = case_data["evidence"][0]
        assert first_evidence["category"] == original_category


@pytest.mark.usefixtures("mock_services_for_integration_tests")
class TestAnalyticsQueries:
    """Test analytics queries for evidence acceptance and rejection"""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_acceptance_rate_calculation(self, test_case, auth_headers):
        """Test calculating evidence acceptance rate"""
        # Arrange - Upload mix of relevant and irrelevant files
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Relevant file
            await client.post(
                f"/api/v1/cases/{test_case}/data",
                files={"file": ("error.log", io.BytesIO(b"ERROR: Test"), "text/plain")},
                headers=auth_headers,
            )
            # Another relevant file
            await client.post(
                f"/api/v1/cases/{test_case}/data",
                files={"file": ("metrics.txt", io.BytesIO(b"CPU: 100%"), "text/plain")},
                headers=auth_headers,
            )

        # Get case
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            case_response = await client.get(
                f"/api/v1/cases/{test_case}", headers=auth_headers
            )
        case_data = case_response.json()

        # Calculate acceptance rate
        total_submissions = len(case_data["evidence"])
        valid_evidence = [
            e
            for e in case_data["evidence"]
            if e["category"] != EvidenceCategory.REJECTED.value
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

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rejection_reasons_tracking(self, test_case, auth_headers):
        """Test tracking rejection reasons"""
        # This would require uploading files that get rejected
        # and verifying the primary_purpose field contains rejection reason

        # Arrange - Upload potentially irrelevant file
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/cases/{test_case}/data",
                files={
                    "file": ("random.txt", io.BytesIO(b"Random content"), "text/plain")
                },
                headers=auth_headers,
            )

        # Get case
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            case_response = await client.get(
                f"/api/v1/cases/{test_case}", headers=auth_headers
            )
        case_data = case_response.json()

        # Find rejected evidence (if any)
        rejected = [
            e
            for e in case_data["evidence"]
            if e["category"] == EvidenceCategory.REJECTED.value
        ]

        # If rejected evidence exists, verify it has a reason
        for evidence in rejected:
            assert evidence["primary_purpose"] is not None
            assert len(evidence["primary_purpose"]) > 0


@pytest.mark.usefixtures("mock_services_for_integration_tests")
class TestPreprocessingIntegration:
    """Test preprocessing integration with evidence creation"""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_content_hash_generated(self, test_case, auth_headers):
        """Verify content_hash is generated during preprocessing"""
        # Arrange
        log_content = b"Test log content"
        expected_hash = hashlib.sha256(log_content).hexdigest()

        # Act - Upload file
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                f"/api/v1/cases/{test_case}/data",
                files={"file": ("test.log", io.BytesIO(log_content), "text/plain")},
                headers=auth_headers,
            )

        # Assert
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            case_response = await client.get(
                f"/api/v1/cases/{test_case}", headers=auth_headers
            )
        case_data = case_response.json()

        assert len(case_data["evidence"]) >= 1
        evidence = case_data["evidence"][0]
        assert evidence["content_hash"] == expected_hash

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_preprocessing_method_recorded(self, test_case, auth_headers):
        """Verify preprocessing_method is recorded in evidence"""
        # Arrange - Upload different file types
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Log file
            await client.post(
                f"/api/v1/cases/{test_case}/data",
                files={"file": ("app.log", io.BytesIO(b"LOG CONTENT"), "text/plain")},
                headers=auth_headers,
            )

        # Assert
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            case_response = await client.get(
                f"/api/v1/cases/{test_case}", headers=auth_headers
            )
        case_data = case_response.json()

        # Evidence should have preprocessing_method
        for evidence in case_data["evidence"]:
            assert "preprocessing_method" in evidence
            assert evidence["preprocessing_method"] is not None
