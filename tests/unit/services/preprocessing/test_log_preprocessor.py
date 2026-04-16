"""Tests for LogPreprocessor - log parsing, security detection, and LLM summary formatting."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.models.api import DataType, SourceMetadata
from faultmaven.services.preprocessing.preprocessors.log_preprocessor import (
    LogPreprocessor,
)


@pytest.fixture
def preprocessor():
    """LogPreprocessor with mocked LogProcessor to isolate formatting/detection logic."""
    with patch(
        "faultmaven.services.preprocessing.preprocessors.log_preprocessor.LogProcessor"
    ) as MockLogProcessor:
        mock_log_proc = AsyncMock()
        mock_log_proc.process = AsyncMock(
            return_value={
                "total_entries": 500,
                "time_range": {
                    "start": "2024-01-01T00:00:00",
                    "end": "2024-01-01T02:00:00",
                    "duration_hours": 2.0,
                },
                "error_summary": {"total_errors": 42, "error_rate": 0.084},
                "log_level_distribution": {"ERROR": 42, "WARN": 100, "INFO": 358},
                "top_errors": [
                    "NullPointerException",
                    "ConnectionTimeout",
                    "OutOfMemoryError",
                ],
                "anomalies": [
                    {
                        "type": "spike",
                        "severity": "high",
                        "description": "Error rate spike at 01:30",
                    }
                ],
            }
        )
        MockLogProcessor.return_value = mock_log_proc
        yield LogPreprocessor()


class TestExtractErrorSamples:
    """Test _extract_error_samples: filters log lines containing error-level keywords."""

    def test_extracts_error_lines(self, preprocessor):
        content = (
            "2024-01-01 INFO Starting service\n"
            "2024-01-01 ERROR Connection refused\n"
            "2024-01-01 WARN Slow query\n"
            "2024-01-01 FATAL OOM killed\n"
            "2024-01-01 CRITICAL Disk full\n"
        )
        samples = preprocessor._extract_error_samples(content, {})
        assert len(samples) == 3
        assert "ERROR Connection refused" in samples[0]
        assert "FATAL OOM killed" in samples[1]
        assert "CRITICAL Disk full" in samples[2]

    def test_caps_at_ten_samples(self, preprocessor):
        lines = [f"2024-01-01 ERROR Error number {i}" for i in range(20)]
        content = "\n".join(lines)
        samples = preprocessor._extract_error_samples(content, {})
        assert len(samples) == 10

    def test_returns_empty_for_info_only_logs(self, preprocessor):
        content = "2024-01-01 INFO All good\n2024-01-01 DEBUG Trace info\n"
        samples = preprocessor._extract_error_samples(content, {})
        assert samples == []

    def test_case_insensitive_match(self, preprocessor):
        content = "error: something went wrong\nFatal crash detected\n"
        samples = preprocessor._extract_error_samples(content, {})
        assert len(samples) == 2


class TestDetectSecurityIssues:
    """Test _detect_security_issues: regex-based detection of secrets/PII in log content."""

    def test_detects_api_key(self, preprocessor):
        content = "api_key=ABCDEF1234567890ABCDEF"
        flags = preprocessor._detect_security_issues(content)
        assert "api_key_detected" in flags

    def test_detects_password(self, preprocessor):
        content = "password: mysecretpassword123"
        flags = preprocessor._detect_security_issues(content)
        assert "password_in_logs" in flags

    def test_detects_jwt_token(self, preprocessor):
        content = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        flags = preprocessor._detect_security_issues(content)
        assert "jwt_token_detected" in flags

    def test_detects_aws_key(self, preprocessor):
        content = "AKIAIOSFODNN7EXAMPLE"
        flags = preprocessor._detect_security_issues(content)
        assert "aws_key_detected" in flags

    def test_detects_email(self, preprocessor):
        content = "User logged in: admin@example.com"
        flags = preprocessor._detect_security_issues(content)
        assert "email_in_logs" in flags

    def test_detects_ip_address(self, preprocessor):
        content = "Connection from 192.168.1.100"
        flags = preprocessor._detect_security_issues(content)
        assert "ip_addresses_in_logs" in flags

    def test_no_flags_for_clean_content(self, preprocessor):
        content = "2024-01-01 INFO Application started successfully"
        flags = preprocessor._detect_security_issues(content)
        assert flags == []

    def test_detects_multiple_issues(self, preprocessor):
        content = "password=secret123 api_key=ABCDEF1234567890ABCDEF"
        flags = preprocessor._detect_security_issues(content)
        assert len(flags) >= 2
        assert "password_in_logs" in flags
        assert "api_key_detected" in flags


class TestFormatLogSummary:
    """Test _format_log_summary: structured plain text output for LLM consumption."""

    def test_includes_header_and_filename(self, preprocessor):
        summary = preprocessor._format_log_summary(
            insights={"total_entries": 100},
            error_samples=[],
            filename="app.log",
            source_metadata=None,
            security_flags=[],
        )
        assert "LOG FILE ANALYSIS SUMMARY" in summary
        assert "Filename: app.log" in summary

    def test_includes_time_range(self, preprocessor):
        insights = {
            "total_entries": 100,
            "time_range": {
                "start": "2024-01-01T00:00:00",
                "end": "2024-01-01T02:00:00",
                "duration_hours": 2.0,
            },
        }
        summary = preprocessor._format_log_summary(
            insights=insights,
            error_samples=[],
            filename="test.log",
            source_metadata=None,
            security_flags=[],
        )
        assert "Duration: 2.0 hours" in summary
        assert "2024-01-01T00:00:00" in summary

    def test_includes_error_samples_truncated(self, preprocessor):
        long_error = "E" * 300
        summary = preprocessor._format_log_summary(
            insights={"total_entries": 10},
            error_samples=[long_error],
            filename="test.log",
            source_metadata=None,
            security_flags=[],
        )
        assert "SAMPLE ERROR MESSAGES" in summary
        # Long lines should be truncated at 200 chars + "..."
        assert "..." in summary

    def test_includes_security_warnings(self, preprocessor):
        summary = preprocessor._format_log_summary(
            insights={"total_entries": 10},
            error_samples=[],
            filename="test.log",
            source_metadata=None,
            security_flags=["api_key_detected", "password_in_logs"],
        )
        assert "SECURITY WARNINGS" in summary
        assert "api_key_detected" in summary

    def test_includes_anomalies(self, preprocessor):
        insights = {
            "total_entries": 100,
            "anomalies": [
                {"type": "spike", "severity": "high", "description": "Error rate spike"}
            ],
        }
        summary = preprocessor._format_log_summary(
            insights=insights,
            error_samples=[],
            filename="test.log",
            source_metadata=None,
            security_flags=[],
        )
        assert "ANOMALIES DETECTED" in summary
        assert "Error rate spike" in summary

    def test_includes_source_metadata(self, preprocessor):
        metadata = SourceMetadata(source_type="file_upload", source_url=None)
        summary = preprocessor._format_log_summary(
            insights={"total_entries": 10},
            error_samples=[],
            filename="test.log",
            source_metadata=metadata,
            security_flags=[],
        )
        assert "SOURCE:" in summary
        assert "Type: file_upload" in summary

    def test_includes_performance_metrics(self, preprocessor):
        insights = {
            "total_entries": 100,
            "performance_metrics": {
                "avg_response_time_ms": 150.5,
                "p95_response_time_ms": 450.2,
                "max_response_time_ms": 1200.0,
            },
        }
        summary = preprocessor._format_log_summary(
            insights=insights,
            error_samples=[],
            filename="test.log",
            source_metadata=None,
            security_flags=[],
        )
        assert "PERFORMANCE METRICS" in summary
        assert "150.50ms" in summary


class TestProcessEndToEnd:
    """Test the full process() pipeline with mocked LogProcessor."""

    @pytest.mark.asyncio
    async def test_successful_processing(self, preprocessor):
        content = "2024-01-01 ERROR Connection refused\n2024-01-01 INFO OK\n"
        result = await preprocessor.process(content, "app.log")

        assert result.metadata.data_type == DataType.LOGS_AND_ERRORS
        assert result.metadata.extraction_strategy == "crime_scene"
        assert result.metadata.llm_calls_used == 0
        assert result.metadata.confidence == 0.95
        assert result.original_size == len(content)
        assert result.processed_size <= 8090
        assert "LOG FILE ANALYSIS SUMMARY" in result.content

    @pytest.mark.asyncio
    async def test_truncates_to_8k_chars(self, preprocessor):
        # Make the mock return insights that produce a very long summary
        preprocessor.log_processor.process.return_value = {
            "total_entries": 999999,
            "top_errors": [f"Error pattern {i}" for i in range(100)],
            "anomalies": [
                {"type": "spike", "severity": "high", "description": "X" * 500}
                for _ in range(50)
            ],
        }
        content = "2024-01-01 ERROR test\n" * 1000
        result = await preprocessor.process(content, "huge.log")
        assert result.processed_size <= 8090

    @pytest.mark.asyncio
    async def test_returns_error_result_on_processor_failure(self, preprocessor):
        preprocessor.log_processor.process.side_effect = RuntimeError("parse failed")
        result = await preprocessor.process("bad data", "broken.log")

        assert result.metadata.confidence == 0.0
        assert result.metadata.extraction_strategy == "none"
        assert "preprocessing_error" in result.security_flags
        assert "parse failed" in result.content

    @pytest.mark.asyncio
    async def test_preserves_insights_in_result(self, preprocessor):
        result = await preprocessor.process("2024-01-01 INFO OK", "app.log")
        assert result.insights is not None
        assert result.insights.get("total_entries") == 500

    @pytest.mark.asyncio
    async def test_with_source_metadata(self, preprocessor):
        metadata = SourceMetadata(
            source_type="page_capture", source_url="https://example.com/logs"
        )
        result = await preprocessor.process("2024-01-01 INFO OK", "app.log", metadata)
        assert result.source_metadata == metadata
        assert "https://example.com/logs" in result.content
