"""Unit tests for Evidence Artifact domain model.

Tests the EvidenceArtifact dataclass and related enums.
"""

import pytest
from datetime import datetime, timezone

from faultmaven.models.evidence_artifact import (
    EvidenceArtifact,
    EvidenceArtifactType,
    StorageBackend,
)


class TestEvidenceArtifactType:
    """Tests for EvidenceArtifactType enum."""

    def test_all_types_defined(self):
        """Verify all expected evidence types are defined."""
        expected_types = [
            "screenshot",
            "log_file",
            "network_trace",
            "code_snippet",
            "configuration",
            "video_recording",
            "har_file",
            "crash_dump",
            "heap_dump",
            "thread_dump",
            "metrics_export",
            "other",
        ]
        actual_types = [t.value for t in EvidenceArtifactType]
        assert set(expected_types) == set(actual_types)

    def test_enum_is_string(self):
        """Verify enum values are strings."""
        for t in EvidenceArtifactType:
            assert isinstance(t.value, str)

    def test_enum_lookup_by_value(self):
        """Test looking up enum by value."""
        assert EvidenceArtifactType("screenshot") == EvidenceArtifactType.SCREENSHOT
        assert EvidenceArtifactType("log_file") == EvidenceArtifactType.LOG_FILE


class TestStorageBackend:
    """Tests for StorageBackend enum."""

    def test_all_backends_defined(self):
        """Verify all expected storage backends are defined."""
        expected_backends = ["local_filesystem", "s3", "azure_blob", "gcs"]
        actual_backends = [b.value for b in StorageBackend]
        assert set(expected_backends) == set(actual_backends)

    def test_enum_lookup_by_value(self):
        """Test looking up enum by value."""
        assert StorageBackend("local_filesystem") == StorageBackend.LOCAL_FILESYSTEM
        assert StorageBackend("s3") == StorageBackend.S3


class TestEvidenceArtifactCreation:
    """Tests for EvidenceArtifact creation and validation."""

    def test_create_minimal_evidence(self):
        """Test creating evidence with minimal required fields."""
        evidence = EvidenceArtifact(
            evidence_id="ev_123abc456def",
            case_id="case_abc123def456",
            user_id="user_001",
            organization_id="org_001",
            original_filename="screenshot.png",
            stored_filename="ev_123abc456def.png",
            file_path="/evidence/2025/12/ev_123abc456def.png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            mime_type="image/png",
            file_size=1024000,
        )

        assert evidence.evidence_id == "ev_123abc456def"
        assert evidence.case_id == "case_abc123def456"
        assert evidence.evidence_type == EvidenceArtifactType.SCREENSHOT
        assert evidence.storage_backend == StorageBackend.LOCAL_FILESYSTEM
        assert evidence.is_primary is False
        assert evidence.description is None
        assert evidence.metadata is None

    def test_create_full_evidence(self):
        """Test creating evidence with all fields."""
        created = datetime(2025, 12, 29, 10, 0, 0, tzinfo=timezone.utc)
        updated = datetime(2025, 12, 29, 11, 0, 0, tzinfo=timezone.utc)

        evidence = EvidenceArtifact(
            evidence_id="ev_full123456",
            case_id="case_abc123def456",
            user_id="user_001",
            organization_id="org_001",
            original_filename="error.log",
            stored_filename="ev_full123456.log",
            file_path="/evidence/2025/12/ev_full123456.log",
            evidence_type=EvidenceArtifactType.LOG_FILE,
            mime_type="text/plain",
            file_size=512000,
            storage_backend=StorageBackend.S3,
            created_at=created,
            updated_at=updated,
            metadata={"source": "kubernetes", "pod": "api-server"},
            description="API server error logs",
            is_primary=True,
        )

        assert evidence.storage_backend == StorageBackend.S3
        assert evidence.created_at == created
        assert evidence.updated_at == updated
        assert evidence.metadata == {"source": "kubernetes", "pod": "api-server"}
        assert evidence.description == "API server error logs"
        assert evidence.is_primary is True

    def test_default_timestamps(self):
        """Test that timestamps are auto-generated if not provided."""
        before = datetime.now(timezone.utc)

        evidence = EvidenceArtifact(
            evidence_id="ev_timestamps",
            case_id="case_123",
            user_id="user_001",
            organization_id="org_001",
            original_filename="test.log",
            stored_filename="test.log",
            file_path="/test.log",
            evidence_type=EvidenceArtifactType.LOG_FILE,
            mime_type="text/plain",
            file_size=100,
        )

        after = datetime.now(timezone.utc)

        assert before <= evidence.created_at <= after
        assert before <= evidence.updated_at <= after


class TestEvidenceArtifactValidation:
    """Tests for EvidenceArtifact validation."""

    def test_empty_evidence_id_fails(self):
        """Test that empty evidence_id raises ValueError."""
        with pytest.raises(ValueError, match="evidence_id is required"):
            EvidenceArtifact(
                evidence_id="",
                case_id="case_123",
                user_id="user_001",
                organization_id="org_001",
                original_filename="test.png",
                stored_filename="test.png",
                file_path="/test.png",
                evidence_type=EvidenceArtifactType.SCREENSHOT,
                mime_type="image/png",
                file_size=100,
            )

    def test_empty_case_id_fails(self):
        """Test that empty case_id raises ValueError."""
        with pytest.raises(ValueError, match="case_id is required"):
            EvidenceArtifact(
                evidence_id="ev_123",
                case_id="",
                user_id="user_001",
                organization_id="org_001",
                original_filename="test.png",
                stored_filename="test.png",
                file_path="/test.png",
                evidence_type=EvidenceArtifactType.SCREENSHOT,
                mime_type="image/png",
                file_size=100,
            )

    def test_empty_user_id_fails(self):
        """Test that empty user_id raises ValueError."""
        with pytest.raises(ValueError, match="user_id is required"):
            EvidenceArtifact(
                evidence_id="ev_123",
                case_id="case_123",
                user_id="",
                organization_id="org_001",
                original_filename="test.png",
                stored_filename="test.png",
                file_path="/test.png",
                evidence_type=EvidenceArtifactType.SCREENSHOT,
                mime_type="image/png",
                file_size=100,
            )

    def test_empty_organization_id_fails(self):
        """Test that empty organization_id raises ValueError."""
        with pytest.raises(ValueError, match="organization_id is required"):
            EvidenceArtifact(
                evidence_id="ev_123",
                case_id="case_123",
                user_id="user_001",
                organization_id="",
                original_filename="test.png",
                stored_filename="test.png",
                file_path="/test.png",
                evidence_type=EvidenceArtifactType.SCREENSHOT,
                mime_type="image/png",
                file_size=100,
            )

    def test_empty_original_filename_fails(self):
        """Test that empty original_filename raises ValueError."""
        with pytest.raises(ValueError, match="original_filename is required"):
            EvidenceArtifact(
                evidence_id="ev_123",
                case_id="case_123",
                user_id="user_001",
                organization_id="org_001",
                original_filename="",
                stored_filename="test.png",
                file_path="/test.png",
                evidence_type=EvidenceArtifactType.SCREENSHOT,
                mime_type="image/png",
                file_size=100,
            )

    def test_empty_file_path_fails(self):
        """Test that empty file_path raises ValueError."""
        with pytest.raises(ValueError, match="file_path is required"):
            EvidenceArtifact(
                evidence_id="ev_123",
                case_id="case_123",
                user_id="user_001",
                organization_id="org_001",
                original_filename="test.png",
                stored_filename="test.png",
                file_path="",
                evidence_type=EvidenceArtifactType.SCREENSHOT,
                mime_type="image/png",
                file_size=100,
            )

    def test_negative_file_size_fails(self):
        """Test that negative file_size raises ValueError."""
        with pytest.raises(ValueError, match="file_size cannot be negative"):
            EvidenceArtifact(
                evidence_id="ev_123",
                case_id="case_123",
                user_id="user_001",
                organization_id="org_001",
                original_filename="test.png",
                stored_filename="test.png",
                file_path="/test.png",
                evidence_type=EvidenceArtifactType.SCREENSHOT,
                mime_type="image/png",
                file_size=-100,
            )

    def test_zero_file_size_allowed(self):
        """Test that zero file_size is allowed."""
        evidence = EvidenceArtifact(
            evidence_id="ev_123",
            case_id="case_123",
            user_id="user_001",
            organization_id="org_001",
            original_filename="empty.txt",
            stored_filename="empty.txt",
            file_path="/empty.txt",
            evidence_type=EvidenceArtifactType.OTHER,
            mime_type="text/plain",
            file_size=0,
        )
        assert evidence.file_size == 0


class TestEvidenceArtifactMethods:
    """Tests for EvidenceArtifact helper methods."""

    @pytest.fixture
    def sample_evidence(self):
        """Create sample evidence for testing."""
        return EvidenceArtifact(
            evidence_id="ev_sample123",
            case_id="case_123",
            user_id="user_001",
            organization_id="org_001",
            original_filename="screenshot.png",
            stored_filename="ev_sample123.png",
            file_path="/evidence/screenshot.png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            mime_type="image/png",
            file_size=1048576,  # 1 MB
        )

    def test_get_display_name_with_description(self, sample_evidence):
        """Test get_display_name returns description when set."""
        sample_evidence.description = "Main bug screenshot"
        assert sample_evidence.get_display_name() == "Main bug screenshot"

    def test_get_display_name_without_description(self, sample_evidence):
        """Test get_display_name returns filename when no description."""
        assert sample_evidence.get_display_name() == "screenshot.png"

    def test_is_image_png(self, sample_evidence):
        """Test is_image for PNG file."""
        assert sample_evidence.is_image() is True

    def test_is_image_jpeg(self, sample_evidence):
        """Test is_image for JPEG file."""
        sample_evidence.mime_type = "image/jpeg"
        assert sample_evidence.is_image() is True

    def test_is_image_text_file(self, sample_evidence):
        """Test is_image returns False for text file."""
        sample_evidence.mime_type = "text/plain"
        assert sample_evidence.is_image() is False

    def test_is_text_plain(self, sample_evidence):
        """Test is_text for plain text file."""
        sample_evidence.mime_type = "text/plain"
        assert sample_evidence.is_text() is True

    def test_is_text_json(self, sample_evidence):
        """Test is_text for JSON file."""
        sample_evidence.mime_type = "application/json"
        assert sample_evidence.is_text() is True

    def test_is_text_xml(self, sample_evidence):
        """Test is_text for XML file."""
        sample_evidence.mime_type = "application/xml"
        assert sample_evidence.is_text() is True

    def test_is_text_image(self, sample_evidence):
        """Test is_text returns False for image file."""
        assert sample_evidence.is_text() is False

    def test_is_video_mp4(self, sample_evidence):
        """Test is_video for MP4 file."""
        sample_evidence.mime_type = "video/mp4"
        assert sample_evidence.is_video() is True

    def test_is_video_image(self, sample_evidence):
        """Test is_video returns False for image file."""
        assert sample_evidence.is_video() is False

    def test_get_extension_png(self, sample_evidence):
        """Test get_extension for PNG file."""
        assert sample_evidence.get_extension() == ".png"

    def test_get_extension_multiple_dots(self, sample_evidence):
        """Test get_extension for file with multiple dots."""
        sample_evidence.original_filename = "log.2025-12-29.txt"
        assert sample_evidence.get_extension() == ".txt"

    def test_get_extension_no_extension(self, sample_evidence):
        """Test get_extension for file without extension."""
        sample_evidence.original_filename = "Makefile"
        assert sample_evidence.get_extension() == ""

    def test_get_extension_uppercase(self, sample_evidence):
        """Test get_extension normalizes to lowercase."""
        sample_evidence.original_filename = "Screenshot.PNG"
        assert sample_evidence.get_extension() == ".png"

    def test_get_size_display_bytes(self, sample_evidence):
        """Test get_size_display for bytes."""
        sample_evidence.file_size = 500
        assert sample_evidence.get_size_display() == "500 B"

    def test_get_size_display_kb(self, sample_evidence):
        """Test get_size_display for kilobytes."""
        sample_evidence.file_size = 1536  # 1.5 KB
        assert sample_evidence.get_size_display() == "1.5 KB"

    def test_get_size_display_mb(self, sample_evidence):
        """Test get_size_display for megabytes."""
        sample_evidence.file_size = 1048576  # 1 MB
        assert sample_evidence.get_size_display() == "1.0 MB"

    def test_get_size_display_gb(self, sample_evidence):
        """Test get_size_display for gigabytes."""
        sample_evidence.file_size = 1073741824  # 1 GB
        assert sample_evidence.get_size_display() == "1.0 GB"

    def test_touch_updates_timestamp(self, sample_evidence):
        """Test that touch() updates updated_at timestamp."""
        original = sample_evidence.updated_at

        # Wait a tiny bit to ensure timestamp difference
        import time
        time.sleep(0.001)

        sample_evidence.touch()

        assert sample_evidence.updated_at > original

    def test_repr(self, sample_evidence):
        """Test string representation."""
        repr_str = repr(sample_evidence)
        assert "EvidenceArtifact" in repr_str
        assert "ev_sample123" in repr_str
        assert "case_123" in repr_str
        assert "screenshot.png" in repr_str
        assert "screenshot" in repr_str
