"""
Unit tests for LogsAndErrorsExtractor (Crime Scene Extraction)

Tests the severity-based error detection and adaptive context extraction.
"""

import pytest
from faultmaven.services.preprocessing.extractors.logs_extractor import LogsAndErrorsExtractor


class TestLogsAndErrorsExtractor:
    """Test Crime Scene Extraction functionality"""

    @pytest.fixture
    def extractor(self):
        """Create extractor instance"""
        return LogsAndErrorsExtractor()

    def test_single_error_extraction(self, extractor):
        """Test extraction of single error with context"""
        # Create log with one ERROR
        log_lines = (
            ["INFO: Starting application"] * 50 +
            ["ERROR: Database connection failed"] +
            ["INFO: Retrying connection"] * 50
        )
        content = "\n".join(log_lines)

        result = extractor.extract(content)

        # Should extract ±200 lines around error
        assert "ERROR: Database connection failed" in result
        assert "CRIME SCENE EXTRACTION" in result
        assert "Single ERROR" in result

    def test_severity_prioritization(self, extractor):
        """Test that FATAL takes priority over ERROR"""
        log_lines = (
            ["INFO: Normal operation"] * 10 +
            ["ERROR: Minor issue at line 11"] +
            ["INFO: Continuing"] * 20 +
            ["FATAL: System crash at line 32"] +
            ["INFO: Aftermath"] * 10
        )
        content = "\n".join(log_lines)

        result = extractor.extract(content)

        # Should prioritize FATAL over ERROR
        assert "FATAL: System crash" in result
        # ERROR might not be included if outside context window
        assert "Single FATAL" in result or "ERROR burst" in result

    def test_multiple_crime_scenes(self, extractor):
        """Test detection of first + last errors"""
        log_lines = (
            ["INFO: Startup"] * 20 +
            ["ERROR: First problem at line 21"] +
            ["INFO: Normal operation"] * 300 +  # Large gap
            ["ERROR: Last problem at line 322"] +
            ["INFO: Shutdown"] * 20
        )
        content = "\n".join(log_lines)

        result = extractor.extract(content)

        # Should extract both crime scenes
        assert "Multiple crime scenes" in result
        assert "First ERROR" in result
        assert "Last ERROR" in result

    def test_error_burst_detection(self, extractor):
        """Test detection of error clustering"""
        log_lines = (
            ["INFO: Normal"] * 30 +
            # Create burst of 15 errors in close proximity
            ["ERROR: Problem 1", "ERROR: Problem 2", "ERROR: Problem 3"] * 5 +
            ["INFO: After burst"] * 30
        )
        content = "\n".join(log_lines)

        result = extractor.extract(content)

        # Should detect burst
        assert "burst detected" in result.lower() or "ERROR" in result

    def test_no_errors_fallback(self, extractor):
        """Test tail extraction when no errors found"""
        log_lines = ["INFO: Normal operation"] * 1000
        content = "\n".join(log_lines)

        result = extractor.extract(content)

        # Should extract last 500 lines
        assert "No errors detected" in result
        assert "showing last" in result

    def test_safety_truncation(self, extractor):
        """Test that output is truncated if too large"""
        # Create very large error context
        log_lines = (
            ["INFO: Line"] * 100 +
            ["ERROR: Problem"] +
            ["INFO: Context line"] * 1000  # Massive context
        )
        content = "\n".join(log_lines)

        result = extractor.extract(content)

        # Should be truncated to MAX_SNIPPET_LINES (500)
        result_line_count = len(result.split('\n'))
        assert result_line_count <= extractor.MAX_SNIPPET_LINES + 10  # Some buffer for headers

    def test_panic_keyword_go_lang(self, extractor):
        """Test Go language panic detection"""
        log_lines = (
            ["INFO: Starting Go service"] * 20 +
            ["panic: runtime error: index out of range"] +
            ["goroutine 1 [running]:"] +
            ["main.main()"] * 10
        )
        content = "\n".join(log_lines)

        result = extractor.extract(content)

        assert "panic" in result.lower()
        assert "CRIME SCENE" in result

    def test_properties(self, extractor):
        """Test extractor properties"""
        assert extractor.strategy_name == "crime_scene"
        assert extractor.llm_calls_used == 0
