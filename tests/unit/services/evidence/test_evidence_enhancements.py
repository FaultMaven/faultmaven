"""
Unit Tests for Evidence Enhancements

Tests the automated evidence processing enhancements:
- Timeline extraction from logs (Gap 1.2)
- Hypothesis generation from anomalies (Gap 1.3)
"""

from datetime import datetime

import pytest

from faultmaven.models.api import DataType, PreprocessedData
from faultmaven.models.investigation import HypothesisStatus
from faultmaven.services.evidence.evidence_enhancements import (
    extract_timeline_events,
    should_populate_timeline,
    generate_hypotheses_from_anomalies,
    should_generate_hypotheses,
)


class TestTimelineExtraction:
    """Test timeline event extraction from preprocessed data"""

    def test_extracts_events_from_log_with_errors(self):
        """Test timeline extraction from logs with errors"""
        preprocessed = PreprocessedData(
            data_id="data_test",
            data_type=DataType.LOG_FILE,
            summary="Log summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={
                "top_errors": [
                    {
                        "message": "Connection timeout",
                        "count": 15,
                        "first_seen": "2025-10-13T10:15:32Z"
                    },
                    {
                        "message": "NullPointerException",
                        "count": 8,
                        "first_seen": "2025-10-13T10:20:45Z"
                    }
                ],
                "time_range": {
                    "start": "2025-10-13T10:15:00Z",
                    "end": "2025-10-13T10:30:00Z"
                }
            },
            security_flags=[],
        )

        events = extract_timeline_events(preprocessed)

        # Should extract: 2 errors + 2 time bounds = 4 events
        assert len(events) >= 2  # At least the top errors

        # Check error events
        error_events = [e for e in events if e["event_type"] == "error"]
        assert len(error_events) >= 2

        # Verify error event structure
        assert error_events[0]["description"] == "Connection timeout"
        assert error_events[0]["severity"] in ["high", "medium"]
        assert "timestamp" in error_events[0]

    def test_extracts_anomaly_events(self):
        """Test extraction of anomaly events from logs"""
        preprocessed = PreprocessedData(
            data_id="data_test",
            data_type=DataType.LOG_FILE,
            summary="Log summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={
                "anomalies": [
                    {
                        "description": "CPU spike to 95%",
                        "timestamp": "2025-10-13T10:18:00Z"
                    },
                    {
                        "description": "Memory usage surge",
                        "timestamp": "2025-10-13T10:19:30Z"
                    }
                ]
            },
            security_flags=[],
        )

        events = extract_timeline_events(preprocessed)

        anomaly_events = [e for e in events if e["event_type"] == "anomaly"]
        assert len(anomaly_events) == 2
        assert anomaly_events[0]["severity"] == "critical"
        assert "CPU spike" in anomaly_events[0]["description"]

    def test_extracts_time_range_bounds(self):
        """Test extraction of time range boundaries"""
        preprocessed = PreprocessedData(
            data_id="data_test",
            data_type=DataType.LOG_FILE,
            summary="Log summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={
                "time_range": {
                    "start": "2025-10-13T10:00:00Z",
                    "end": "2025-10-13T11:00:00Z"
                }
            },
            security_flags=[],
        )

        events = extract_timeline_events(preprocessed)

        bound_events = [e for e in events if e["event_type"] == "state_change"]
        assert len(bound_events) == 2  # start and end
        assert any("start" in e["description"] for e in bound_events)
        assert any("end" in e["description"] for e in bound_events)

    def test_returns_empty_for_no_insights(self):
        """Test returns empty list when no insights"""
        preprocessed = PreprocessedData(
            data_id="data_test",
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

        events = extract_timeline_events(preprocessed)
        assert events == []

    def test_should_populate_timeline_for_logs(self):
        """Test timeline population decision for logs"""
        preprocessed = PreprocessedData(
            data_id="data_test",
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

        assert should_populate_timeline(preprocessed) is True

    def test_should_not_populate_timeline_for_config(self):
        """Test timeline not populated for config files"""
        preprocessed = PreprocessedData(
            data_id="data_test",
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

        assert should_populate_timeline(preprocessed) is False


class TestHypothesisGeneration:
    """Test hypothesis generation from anomalies"""

    def test_generates_hypotheses_from_log_anomalies(self):
        """Test hypothesis generation from log anomalies"""
        preprocessed = PreprocessedData(
            data_id="data_test123",
            data_type=DataType.LOG_FILE,
            summary="Log summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={
                "anomalies": [
                    "CPU spike to 98%",
                    "Memory exhaustion detected"
                ]
            },
            security_flags=[],
        )

        hypotheses = generate_hypotheses_from_anomalies(preprocessed, current_turn=3)

        assert len(hypotheses) == 2
        assert all(h.status == HypothesisStatus.PENDING for h in hypotheses)
        assert all(h.created_at_turn == 3 for h in hypotheses)

        # Check hypothesis linked to source evidence
        assert all(preprocessed.data_id in h.supporting_evidence for h in hypotheses)

        # Check categorization
        assert any(h.category == "infrastructure" for h in hypotheses)

    def test_generates_hypothesis_from_frequent_errors(self):
        """Test hypothesis from high-frequency error pattern"""
        preprocessed = PreprocessedData(
            data_id="data_test456",
            data_type=DataType.LOG_FILE,
            summary="Log summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={
                "top_errors": [
                    {
                        "message": "NullPointerException in UserService",
                        "count": 42
                    }
                ]
            },
            security_flags=[],
        )

        hypotheses = generate_hypotheses_from_anomalies(preprocessed, current_turn=5)

        assert len(hypotheses) == 1
        assert "42 occurrences" in hypotheses[0].statement or "42 times" in hypotheses[0].statement
        assert hypotheses[0].category == "code"  # Null errors are code issues
        assert hypotheses[0].likelihood > 0.4  # Significant confidence

    def test_generates_hypothesis_from_error_report(self):
        """Test hypothesis from stack trace analysis"""
        preprocessed = PreprocessedData(
            data_id="data_test789",
            data_type=DataType.ERROR_REPORT,
            summary="Error summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={
                "exception_type": "FileNotFoundException",
                "root_cause": "Config file missing at /etc/app/settings.json"
            },
            security_flags=[],
        )

        hypotheses = generate_hypotheses_from_anomalies(preprocessed, current_turn=2)

        assert len(hypotheses) == 1
        assert "Config file missing" in hypotheses[0].statement
        assert hypotheses[0].category == "config"
        assert hypotheses[0].likelihood >= 0.6  # High confidence for stack traces

    def test_limits_hypotheses_to_top_3_anomalies(self):
        """Test that only top 3 anomalies generate hypotheses"""
        preprocessed = PreprocessedData(
            data_id="data_test",
            data_type=DataType.LOG_FILE,
            summary="Log summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={
                "anomalies": [
                    "Anomaly 1",
                    "Anomaly 2",
                    "Anomaly 3",
                    "Anomaly 4",
                    "Anomaly 5"
                ]
            },
            security_flags=[],
        )

        hypotheses = generate_hypotheses_from_anomalies(preprocessed, current_turn=1)

        # Should generate at most 3 from anomalies
        assert len(hypotheses) <= 3

    def test_should_generate_for_significant_errors(self):
        """Test decision to generate hypotheses for significant errors"""
        preprocessed = PreprocessedData(
            data_id="data_test",
            data_type=DataType.LOG_FILE,
            summary="Log summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={
                "top_errors": [
                    {"message": "Connection failed", "count": 10}
                ]
            },
            security_flags=[],
        )

        assert should_generate_hypotheses(preprocessed) is True

    def test_should_not_generate_for_low_frequency_errors(self):
        """Test no hypothesis generation for infrequent errors"""
        preprocessed = PreprocessedData(
            data_id="data_test",
            data_type=DataType.LOG_FILE,
            summary="Log summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={
                "top_errors": [
                    {"message": "Minor warning", "count": 2}
                ]
            },
            security_flags=[],
        )

        assert should_generate_hypotheses(preprocessed) is False

    def test_should_generate_for_anomalies(self):
        """Test hypothesis generation triggered by anomalies"""
        preprocessed = PreprocessedData(
            data_id="data_test",
            data_type=DataType.LOG_FILE,
            summary="Log summary",
            original_size=1000,
            summary_size=500,
            compression_ratio=2.0,
            processed_at=datetime.utcnow(),
            processing_time_ms=50.0,
            insights={
                "anomalies": ["CPU spike detected"]
            },
            security_flags=[],
        )

        assert should_generate_hypotheses(preprocessed) is True


class TestHypothesisCategorization:
    """Test hypothesis categorization logic"""

    def test_categorizes_infrastructure_issues(self):
        """Test infrastructure category detection"""
        from faultmaven.services.evidence.evidence_enhancements import _categorize_hypothesis

        assert _categorize_hypothesis("Memory exhaustion") == "infrastructure"
        assert _categorize_hypothesis("CPU spike to 95%") == "infrastructure"
        assert _categorize_hypothesis("Network timeout") == "infrastructure"
        assert _categorize_hypothesis("Disk full") == "infrastructure"

    def test_categorizes_code_issues(self):
        """Test code category detection"""
        from faultmaven.services.evidence.evidence_enhancements import _categorize_hypothesis

        assert _categorize_hypothesis("NullPointerException") == "code"
        assert _categorize_hypothesis("Assertion failed") == "code"
        assert _categorize_hypothesis("Bug in parser") == "code"

    def test_categorizes_config_issues(self):
        """Test config category detection"""
        from faultmaven.services.evidence.evidence_enhancements import _categorize_hypothesis

        assert _categorize_hypothesis("Missing configuration file") == "config"
        assert _categorize_hypothesis("Environment variable not set") == "config"

    def test_categorizes_data_issues(self):
        """Test data category detection"""
        from faultmaven.services.evidence.evidence_enhancements import _categorize_hypothesis

        assert _categorize_hypothesis("Database query timeout") == "data"
        assert _categorize_hypothesis("Schema validation failed") == "data"

    def test_categorizes_external_issues(self):
        """Test external dependency category detection"""
        from faultmaven.services.evidence.evidence_enhancements import _categorize_hypothesis

        assert _categorize_hypothesis("API call failed") == "external"
        assert _categorize_hypothesis("Third-party service unavailable") == "external"

    def test_returns_unknown_for_unrecognized(self):
        """Test unknown category for unrecognized patterns"""
        from faultmaven.services.evidence.evidence_enhancements import _categorize_hypothesis

        assert _categorize_hypothesis("Something weird happened") == "unknown"
