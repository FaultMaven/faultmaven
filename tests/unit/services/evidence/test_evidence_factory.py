"""
Unit Tests for Evidence Factory

Tests the bridge between preprocessing and evidence systems:
- DataType → EvidenceCategory mapping
- PreprocessedData → EvidenceProvided conversion
- Evidence request matching
"""

from datetime import datetime
from uuid import uuid4

import pytest

from faultmaven.models.api import DataType, PreprocessedData, SourceMetadata
from faultmaven.models.evidence import (
    CompletenessLevel,
    EvidenceCategory,
    EvidenceForm,
    EvidenceProvided,
    EvidenceType,
    UserIntent,
)
from faultmaven.services.evidence.evidence_factory import (
    create_evidence_from_preprocessed,
    map_datatype_to_evidence_category,
    match_evidence_to_requests,
)


class TestDataTypeToEvidenceCategoryMapping:
    """Test mapping from DataType to EvidenceCategory"""

    def test_log_file_maps_to_symptoms(self):
        """Log files are symptoms (errors, failures)"""
        assert map_datatype_to_evidence_category(DataType.LOG_FILE) == EvidenceCategory.SYMPTOMS

    def test_error_report_maps_to_symptoms(self):
        """Stack traces are symptom manifestations"""
        assert map_datatype_to_evidence_category(DataType.ERROR_REPORT) == EvidenceCategory.SYMPTOMS

    def test_config_file_maps_to_configuration(self):
        """Config files map to configuration category"""
        assert map_datatype_to_evidence_category(DataType.CONFIG_FILE) == EvidenceCategory.CONFIGURATION

    def test_metrics_data_maps_to_metrics(self):
        """Performance metrics map to metrics category"""
        assert map_datatype_to_evidence_category(DataType.METRICS_DATA) == EvidenceCategory.METRICS

    def test_profiling_data_maps_to_metrics(self):
        """Profiling data is performance metrics"""
        assert map_datatype_to_evidence_category(DataType.PROFILING_DATA) == EvidenceCategory.METRICS

    def test_trace_data_maps_to_timeline(self):
        """Distributed traces show temporal causality"""
        assert map_datatype_to_evidence_category(DataType.TRACE_DATA) == EvidenceCategory.TIMELINE

    def test_documentation_maps_to_environment(self):
        """Documentation provides environment context"""
        assert map_datatype_to_evidence_category(DataType.DOCUMENTATION) == EvidenceCategory.ENVIRONMENT

    def test_screenshot_maps_to_symptoms(self):
        """Screenshots are visual symptom evidence"""
        assert map_datatype_to_evidence_category(DataType.SCREENSHOT) == EvidenceCategory.SYMPTOMS

    def test_other_maps_to_symptoms_as_default(self):
        """Unknown types default to symptoms"""
        assert map_datatype_to_evidence_category(DataType.OTHER) == EvidenceCategory.SYMPTOMS


class TestCreateEvidenceFromPreprocessed:
    """Test conversion of PreprocessedData to EvidenceProvided"""

    @pytest.fixture
    def log_preprocessed_data(self):
        """Sample preprocessed log file"""
        return PreprocessedData(
            data_id="data_abc123",
            data_type=DataType.LOG_FILE,
            summary="LOG FILE ANALYSIS\n\nERROR SUMMARY:\n- 127 errors\n- Most frequent: NullPointerException",
            original_size=50000,
            summary_size=8000,
            compression_ratio=6.25,
            processed_at=datetime(2025, 10, 13, 10, 30, 0),
            processing_time_ms=157.8,
            insights={
                "error_count": 127,
                "top_errors": [{"message": "NullPointerException", "count": 45}],
                "anomalies": ["Spike at 10:15 AM"],
            },
            security_flags=["potential_api_key"],
            source_metadata=None,
        )

    @pytest.fixture
    def error_preprocessed_data(self):
        """Sample preprocessed error report"""
        return PreprocessedData(
            data_id="data_xyz789",
            data_type=DataType.ERROR_REPORT,
            summary="ERROR REPORT\n\nException: ValueError\nRoot cause: Invalid input format",
            original_size=2500,
            summary_size=800,
            compression_ratio=3.13,
            processed_at=datetime(2025, 10, 13, 10, 35, 0),
            processing_time_ms=45.2,
            insights={
                "exception_type": "ValueError",
                "root_cause": "Invalid input format in user_service.py:142",
                "affected_files": ["user_service.py", "validator.py"],
            },
            security_flags=[],
            source_metadata=None,
        )

    def test_creates_evidence_from_log_file(self, log_preprocessed_data):
        """Test evidence creation from log preprocessing"""
        evidence = create_evidence_from_preprocessed(
            preprocessed=log_preprocessed_data,
            filename="app.log",
            turn_number=3,
            evidence_type=EvidenceType.SUPPORTIVE,
        )

        # Basic fields
        assert evidence.evidence_id == "data_abc123"
        assert evidence.turn_number == 3
        assert evidence.form == EvidenceForm.DOCUMENT
        assert evidence.evidence_type == EvidenceType.SUPPORTIVE
        assert evidence.user_intent == UserIntent.PROVIDING_EVIDENCE
        assert evidence.completeness == CompletenessLevel.COMPLETE

        # Content
        assert "LOG FILE ANALYSIS" in evidence.content
        assert "127 errors" in evidence.content

        # File metadata
        assert evidence.file_metadata is not None
        assert evidence.file_metadata.filename == "app.log"
        assert evidence.file_metadata.size_bytes == 50000
        assert evidence.file_metadata.file_id == "data_abc123"
        assert evidence.file_metadata.content_type == "text/plain"

        # Key findings extracted
        assert len(evidence.key_findings) > 0
        assert any("127 errors" in f for f in evidence.key_findings)

    def test_creates_evidence_from_error_report(self, error_preprocessed_data):
        """Test evidence creation from error preprocessing"""
        evidence = create_evidence_from_preprocessed(
            preprocessed=error_preprocessed_data,
            filename="error.txt",
            turn_number=5,
            evidence_type=EvidenceType.REFUTING,
        )

        # Basic fields
        assert evidence.evidence_id == "data_xyz789"
        assert evidence.turn_number == 5
        assert evidence.evidence_type == EvidenceType.REFUTING
        assert evidence.completeness == CompletenessLevel.COMPLETE

        # File metadata
        assert evidence.file_metadata.filename == "error.txt"
        assert evidence.file_metadata.content_type == "text/plain"

        # Key findings
        assert len(evidence.key_findings) > 0
        assert any("ValueError" in f for f in evidence.key_findings)

    def test_addresses_requests_parameter(self, log_preprocessed_data):
        """Test that addresses_requests parameter is passed through"""
        evidence = create_evidence_from_preprocessed(
            preprocessed=log_preprocessed_data,
            filename="app.log",
            turn_number=1,
            addresses_requests=["req-001", "req-002"],
        )

        assert evidence.addresses_requests == ["req-001", "req-002"]

    def test_default_empty_addresses_requests(self, log_preprocessed_data):
        """Test default empty addresses_requests"""
        evidence = create_evidence_from_preprocessed(
            preprocessed=log_preprocessed_data,
            filename="app.log",
            turn_number=1,
        )

        assert evidence.addresses_requests == []

    def test_preserves_timestamp(self, log_preprocessed_data):
        """Test that processed_at timestamp is preserved"""
        evidence = create_evidence_from_preprocessed(
            preprocessed=log_preprocessed_data,
            filename="app.log",
            turn_number=1,
        )

        assert evidence.timestamp == log_preprocessed_data.processed_at

    def test_metrics_data_gets_csv_mime_type(self):
        """Test that metrics data gets appropriate MIME type"""
        preprocessed = PreprocessedData(
            data_id="data_metrics",
            data_type=DataType.METRICS_DATA,
            summary="Metrics summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={},
            security_flags=[],
        )

        evidence = create_evidence_from_preprocessed(
            preprocessed=preprocessed,
            filename="metrics.csv",
            turn_number=1,
        )

        assert evidence.file_metadata.content_type == "text/csv"

    def test_screenshot_gets_image_mime_type(self):
        """Test that screenshots get image MIME type"""
        preprocessed = PreprocessedData(
            data_id="data_screenshot",
            data_type=DataType.SCREENSHOT,
            summary="Screenshot analysis",
            original_size=50000,
            summary_size=200,
            compression_ratio=250.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=100.0,
            insights={},
            security_flags=[],
        )

        evidence = create_evidence_from_preprocessed(
            preprocessed=preprocessed,
            filename="error_screen.png",
            turn_number=1,
        )

        assert evidence.file_metadata.content_type == "image/png"


class TestMatchEvidenceToRequests:
    """Test evidence request matching logic"""

    @pytest.fixture
    def pending_requests(self):
        """Sample pending evidence requests"""
        return [
            {
                "request_id": "req-symptoms-1",
                "category": "symptoms",
                "label": "Recent error logs",
            },
            {
                "request_id": "req-metrics-1",
                "category": "metrics",
                "label": "CPU usage data",
            },
            {
                "request_id": "req-config-1",
                "category": "configuration",
                "label": "Database config",
            },
        ]

    def test_matches_log_to_symptoms_request(self, pending_requests):
        """Test that log file matches symptoms request"""
        preprocessed = PreprocessedData(
            data_id="data_log",
            data_type=DataType.LOG_FILE,
            summary="Log summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={},
            security_flags=[],
        )

        matched = match_evidence_to_requests(preprocessed, pending_requests)

        assert "req-symptoms-1" in matched
        assert "req-metrics-1" not in matched
        assert "req-config-1" not in matched

    def test_matches_metrics_to_metrics_request(self, pending_requests):
        """Test that metrics data matches metrics request"""
        preprocessed = PreprocessedData(
            data_id="data_metrics",
            data_type=DataType.METRICS_DATA,
            summary="Metrics summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={},
            security_flags=[],
        )

        matched = match_evidence_to_requests(preprocessed, pending_requests)

        assert "req-metrics-1" in matched
        assert "req-symptoms-1" not in matched

    def test_matches_config_to_configuration_request(self, pending_requests):
        """Test that config file matches configuration request"""
        preprocessed = PreprocessedData(
            data_id="data_config",
            data_type=DataType.CONFIG_FILE,
            summary="Config summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={},
            security_flags=[],
        )

        matched = match_evidence_to_requests(preprocessed, pending_requests)

        assert "req-config-1" in matched
        assert "req-symptoms-1" not in matched

    def test_returns_empty_list_when_no_requests(self):
        """Test that empty list is returned when no requests"""
        preprocessed = PreprocessedData(
            data_id="data_log",
            data_type=DataType.LOG_FILE,
            summary="Log summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={},
            security_flags=[],
        )

        matched = match_evidence_to_requests(preprocessed, [])

        assert matched == []

    def test_returns_empty_list_when_no_matching_category(self):
        """Test that empty list is returned when no matching category"""
        preprocessed = PreprocessedData(
            data_id="data_trace",
            data_type=DataType.TRACE_DATA,  # Maps to TIMELINE
            summary="Trace summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={},
            security_flags=[],
        )

        # No timeline requests in pending_requests
        pending = [
            {"request_id": "req-1", "category": "symptoms"},
            {"request_id": "req-2", "category": "metrics"},
        ]

        matched = match_evidence_to_requests(preprocessed, pending)

        assert matched == []


class TestKeyFindingsExtraction:
    """Test key findings extraction from insights"""

    def test_extracts_log_findings(self):
        """Test extraction of log-specific findings"""
        preprocessed = PreprocessedData(
            data_id="data_log",
            data_type=DataType.LOG_FILE,
            summary="Log summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={
                "error_count": 42,
                "top_errors": [{"message": "Connection timeout", "count": 12}],
                "anomalies": ["Spike detected", "Unusual pattern"],
            },
            security_flags=[],
        )

        evidence = create_evidence_from_preprocessed(
            preprocessed=preprocessed, filename="app.log", turn_number=1
        )

        assert len(evidence.key_findings) > 0
        assert any("42 errors" in f for f in evidence.key_findings)
        assert any("Connection timeout" in f for f in evidence.key_findings)
        assert any("2 anomalies" in f for f in evidence.key_findings)

    def test_extracts_error_findings(self):
        """Test extraction of error-specific findings"""
        preprocessed = PreprocessedData(
            data_id="data_error",
            data_type=DataType.ERROR_REPORT,
            summary="Error summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={
                "exception_type": "FileNotFoundError",
                "root_cause": "Missing config file at /etc/app/config.json",
                "affected_files": ["main.py", "config_loader.py"],
            },
            security_flags=[],
        )

        evidence = create_evidence_from_preprocessed(
            preprocessed=preprocessed, filename="error.txt", turn_number=1
        )

        assert len(evidence.key_findings) > 0
        assert any("FileNotFoundError" in f for f in evidence.key_findings)
        assert any("Missing config file" in f for f in evidence.key_findings)
        assert any("2 files" in f for f in evidence.key_findings)

    def test_limits_to_5_findings(self):
        """Test that findings are limited to 5"""
        preprocessed = PreprocessedData(
            data_id="data_log",
            data_type=DataType.LOG_FILE,
            summary="Log summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={
                "error_count": 100,
                "top_errors": [
                    {"message": "Error 1", "count": 10},
                    {"message": "Error 2", "count": 9},
                    {"message": "Error 3", "count": 8},
                ],
                "anomalies": ["Anomaly 1", "Anomaly 2", "Anomaly 3"],
            },
            security_flags=[],
        )

        evidence = create_evidence_from_preprocessed(
            preprocessed=preprocessed, filename="app.log", turn_number=1
        )

        assert len(evidence.key_findings) <= 5
