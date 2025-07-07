import pandas as pd
import pytest

from faultmaven.data_processing.log_processor import LogProcessor
from faultmaven.models import AgentState, DataInsightsResponse, DataType


@pytest.fixture
def processor():
    """Create LogProcessor instance for testing."""
    return LogProcessor()


@pytest.fixture
def sample_agent_state():
    """Sample agent state for testing."""
    return AgentState(
        session_id="test-session-123",
        user_query="Database connection issues with auth-service",
        current_phase="formulate_hypothesis",
        investigation_context={
            "keywords": ["database", "connection", "auth-service"],
            "services": ["auth-service"],
            "components": ["database", "auth"],
        },
        findings=[],
        recommendations=[],
        confidence_score=0.8,
        tools_used=["log_processor"],
    )


@pytest.fixture
def sample_unstructured_logs():
    """Sample unstructured log content for testing."""
    return """
2024-01-01 12:00:00 ERROR Database connection failed
2024-01-01 12:00:01 INFO Application started
2024-01-01 12:00:02 WARN High memory usage detected
2024-01-01 12:00:03 ERROR Timeout occurred
2024-01-01 12:00:04 DEBUG Processing request
"""


@pytest.fixture
def sample_structured_logs():
    """Sample structured log content for testing."""
    return """
2024-01-01 12:00:00 [ERROR] [service:api] Database connection failed - status:500 - ip:192.168.1.100
2024-01-01 12:00:01 [INFO] [service:api] Application started - status:200 - ip:192.168.1.100
2024-01-01 12:00:02 [WARN] [service:api] High memory usage detected - status:200 - ip:192.168.1.100
2024-01-01 12:00:03 [ERROR] [service:api] Timeout occurred - status:408 - ip:192.168.1.100
"""


class TestLogProcessor:
    """Test suite for LogProcessor class."""

    def test_init_default_configuration(self, processor):
        """Test LogProcessor initialization with default configuration."""
        assert processor.log_patterns is not None
        assert "timestamp" in processor.log_patterns
        assert "log_level" in processor.log_patterns
        assert processor.compiled_patterns is not None

    def test_parse_unstructured_logs(self, processor, sample_unstructured_logs):
        """Test parsing of unstructured log text."""
        df = processor._parse_logs_to_dataframe(sample_unstructured_logs)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 5
        assert "timestamp" in df.columns
        assert "log_level" in df.columns
        assert "message" in df.columns

        # Check that timestamps are parsed correctly (as strings, not datetime)
        assert df["timestamp"].dtype == "object"  # Timestamps are stored as strings
        assert df["timestamp"].iloc[0] == "2024-01-01 12:00:00"
        assert df["log_level"].iloc[0] == "ERROR"

    def test_parse_structured_logs(self, processor, sample_structured_logs):
        """Test parsing of structured log text."""
        df = processor._parse_logs_to_dataframe(sample_structured_logs)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 4
        assert "timestamp" in df.columns
        assert "log_level" in df.columns
        assert "message" in df.columns
        assert "http_status" in df.columns
        assert "ip_address" in df.columns

        # Check that structured data is extracted (values might be int or str)
        assert df["http_status"].iloc[0] in [500, "500"]
        assert df["ip_address"].iloc[0] == "192.168.1.100"

    def test_parse_log_line_with_timestamp(self, processor):
        """Test parsing a single log line with timestamp."""
        line = "2024-01-01 12:00:00 ERROR Database connection failed"
        entry = processor._parse_log_line(line, 1)

        assert entry is not None
        assert entry["timestamp"] == "2024-01-01 12:00:00"
        assert entry["log_level"] == "ERROR"
        assert entry["message"] == line

    def test_parse_log_line_without_timestamp(self, processor):
        """Test parsing a single log line without timestamp."""
        line = "ERROR Database connection failed"
        entry = processor._parse_log_line(line, 1)

        assert entry is not None
        assert entry["timestamp"] is None
        assert entry["log_level"] == "ERROR"
        assert entry["message"] == line

    def test_parse_log_line_with_http_status(self, processor):
        """Test parsing a log line with HTTP status."""
        line = "2024-01-01 12:00:00 ERROR Request failed - status:500"
        entry = processor._parse_log_line(line, 1)

        assert entry is not None
        assert entry["http_status"] in [500, "500"]

    def test_parse_log_line_with_ip_address(self, processor):
        """Test parsing a log line with IP address."""
        line = "2024-01-01 12:00:00 INFO Request from 192.168.1.100"
        entry = processor._parse_log_line(line, 1)

        assert entry is not None
        assert entry["ip_address"] == "192.168.1.100"

    def test_parse_log_line_with_error_code(self, processor):
        """Test parsing a log line with error code."""
        line = "2024-01-01 12:00:00 ERROR DATABASE_CONNECTION_ERROR occurred"
        entry = processor._parse_log_line(line, 1)

        assert entry is not None
        assert entry["error_code"] == "DATABASE_CONNECTION_ERROR"

    def test_parse_log_line_with_duration(self, processor):
        """Test parsing a log line with duration."""
        line = "2024-01-01 12:00:00 INFO Request completed in 150ms"
        entry = processor._parse_log_line(line, 1)

        assert entry is not None
        assert entry["duration_ms"] in [150, 150.0, "150"]

    def test_extract_basic_insights(
        self, processor, sample_unstructured_logs, sample_agent_state
    ):
        """Test extraction of basic insights from logs."""
        df = processor._parse_logs_to_dataframe(sample_unstructured_logs)
        insights = processor._extract_basic_insights(df, sample_agent_state)

        assert isinstance(insights, dict)
        assert "log_level_distribution" in insights
        assert "error_summary" in insights
        assert "time_range" in insights
        assert "contextual_analysis" in insights

        # Check log level distribution
        assert insights["log_level_distribution"]["ERROR"] == 2
        assert insights["log_level_distribution"]["INFO"] == 1
        assert insights["log_level_distribution"]["WARN"] == 1
        assert insights["log_level_distribution"]["DEBUG"] == 1

    def test_extract_basic_insights_with_context(self, processor, sample_agent_state):
        """Test extraction of basic insights with contextual analysis."""
        # Create logs with context-relevant content
        logs_with_context = """
2024-01-01 12:00:00 ERROR auth-service database connection failed
2024-01-01 12:00:01 INFO Application started
2024-01-01 12:00:02 ERROR auth-service timeout
2024-01-01 12:00:03 DEBUG Processing request
"""
        df = processor._parse_logs_to_dataframe(logs_with_context)
        insights = processor._extract_basic_insights(df, sample_agent_state)

        assert "contextual_analysis" in insights
        contextual = insights["contextual_analysis"]
        assert "context_keywords" in contextual
        assert "contextual_entries" in contextual
        assert (
            contextual["contextual_entries"] > 0
        )  # Should find context-relevant entries

    def test_detect_anomalies(self, processor, sample_unstructured_logs):
        """Test anomaly detection in logs."""
        df = processor._parse_logs_to_dataframe(sample_unstructured_logs)
        anomalies = processor._detect_anomalies(df)

        # Anomaly detection might not find anomalies in this small dataset
        assert isinstance(anomalies, list)
        # The test should pass even if no anomalies are detected

    def test_generate_recommendations(self, processor, sample_agent_state):
        """Test generation of recommendations."""
        insights = {
            "log_level_distribution": {"ERROR": 5, "INFO": 10},
            "error_summary": {"total_errors": 5, "error_rate": 0.33},
            "contextual_analysis": {"contextual_entries": 2, "contextual_errors": 1},
        }
        anomalies = []

        recommendations = processor._generate_recommendations(
            insights, anomalies, sample_agent_state
        )

        assert isinstance(recommendations, list)
        # Should generate some recommendations based on insights
        assert len(recommendations) > 0

    def test_generate_phase_specific_recommendations(
        self, processor, sample_agent_state
    ):
        """Test generation of phase-specific recommendations."""
        insights = {
            "contextual_analysis": {
                "contextual_entries": 10,
                "contextual_percentage": 25.0,
                "contextual_errors": 3,  # Add this to trigger hypothesis recommendations
            },
            "time_range": {
                "start": "2024-01-01T12:00:00",
                "end": "2024-01-01T13:00:00",
                "duration_hours": 1.0,
            },
            "error_summary": {
                "total_errors": 5,
                "error_rate": 0.05,  # Keep this low to avoid other recommendations
            },
        }
        anomalies = []

        recommendations = processor._generate_recommendations(
            insights, anomalies, sample_agent_state
        )

        assert isinstance(recommendations, list)
        assert len(recommendations) > 0
        # Should contain phase-specific recommendations for formulate_hypothesis phase
        phase_specific = any("Hypothesis" in rec for rec in recommendations)
        assert phase_specific

    def test_calculate_confidence(
        self, processor, sample_unstructured_logs, sample_agent_state
    ):
        """Test confidence score calculation."""
        df = processor._parse_logs_to_dataframe(sample_unstructured_logs)
        insights = processor._extract_basic_insights(df, sample_agent_state)
        anomalies = processor._detect_anomalies(df)

        confidence = processor._calculate_confidence(df, insights, anomalies)

        assert isinstance(confidence, float)
        assert 0.0 <= confidence <= 1.0

    @pytest.mark.asyncio
    async def test_process_success(
        self, processor, sample_unstructured_logs, sample_agent_state
    ):
        """Test successful log processing."""
        data_id = "test-data-123"
        result = await processor.process(
            sample_unstructured_logs, data_id, sample_agent_state
        )

        assert result.data_id == data_id
        assert result.data_type == DataType.LOG_FILE
        assert result.confidence_score >= 0.0
        assert result.processing_time_ms >= 0
        assert isinstance(result.insights, dict)
        assert isinstance(result.anomalies_detected, list)
        assert isinstance(result.recommendations, list)

    @pytest.mark.asyncio
    async def test_process_empty_content(self, processor, sample_agent_state):
        """Test processing empty log content."""
        data_id = "test-data-empty"
        result = await processor.process("", data_id, sample_agent_state)

        assert result.data_id == data_id
        assert result.data_type == DataType.LOG_FILE
        assert result.confidence_score == 0.0
        assert "error" in result.insights

    @pytest.mark.asyncio
    async def test_process_invalid_content(self, processor, sample_agent_state):
        """Test processing invalid log content."""
        data_id = "test-data-invalid"
        result = await processor.process(
            "Invalid log format", data_id, sample_agent_state
        )

        assert result.data_id == data_id
        assert result.data_type == DataType.LOG_FILE
        assert result.confidence_score >= 0.0

    def test_extract_insights_with_missing_columns(self, processor, sample_agent_state):
        """Test insight extraction with missing DataFrame columns."""
        # Create a minimal DataFrame
        df = pd.DataFrame(
            {
                "timestamp": ["2024-01-01 12:00:00"],
                "log_level": ["ERROR"],
                "message": ["Test error"],
            }
        )

        insights = processor._extract_basic_insights(df, sample_agent_state)

        assert isinstance(insights, dict)
        assert "log_level_distribution" in insights
        assert "error_summary" in insights
        assert "time_range" in insights
        assert "contextual_analysis" in insights

        # Should handle missing columns gracefully
        assert insights["log_level_distribution"]["ERROR"] == 1

    def test_parse_logs_with_mixed_formats(self, processor):
        """Test parsing logs with mixed formats."""
        mixed_logs = """
2024-01-01 12:00:00 ERROR Database connection failed
[INFO] Application started at 2024-01-01 12:00:01
2024-01-01 12:00:02 WARN High memory usage
"""

        df = processor._parse_logs_to_dataframe(mixed_logs)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        # Should handle mixed formats gracefully
        assert "timestamp" in df.columns
        assert "log_level" in df.columns

    def test_parse_empty_logs(self, processor):
        """Test parsing empty log content."""
        df = processor._parse_logs_to_dataframe("")
        assert df.empty

    def test_parse_malformed_json(self, processor):
        """Test parsing malformed JSON logs."""
        malformed_logs = """
{"timestamp": "2024-01-01T12:00:00Z", "level": "ERROR", "message": "Valid JSON"}
{"timestamp": "2024-01-01T12:00:01Z", "level": "INFO", "message": "Valid JSON"}
{"timestamp": "2024-01-01T12:00:02Z", "level": "WARN", "message": "Invalid JSON
"""

        df = processor._parse_logs_to_dataframe(malformed_logs)

        # Should handle malformed JSON gracefully
        assert isinstance(df, pd.DataFrame)
        assert len(df) >= 2  # Should parse valid lines

    def test_detect_anomalies_no_anomalies(self, processor):
        """Test anomaly detection with normal data."""
        # Create normal data
        normal_logs = "\n".join(
            [f"2024-01-01 12:00:{i:02d} INFO Normal message {i}" for i in range(10)]
        )

        df = processor._parse_logs_to_dataframe(normal_logs)
        anomalies = processor._detect_anomalies(df)

        # Should handle normal data gracefully
        assert isinstance(anomalies, list)

    def test_suggest_next_action(self, processor, sample_agent_state):
        """Test next action suggestion logic."""
        # Test with high error rate
        high_error_insights = {
            "log_level_distribution": {"ERROR": 10, "INFO": 10},
            "error_summary": {"total_errors": 10, "error_rate": 0.5},
        }
        recommendations = processor._generate_recommendations(
            high_error_insights, [], sample_agent_state
        )

        # Should generate recommendations based on high error rate
        assert isinstance(recommendations, list)
        assert len(recommendations) > 0

    @pytest.mark.asyncio
    async def test_process_complete_flow(
        self, processor, sample_unstructured_logs, sample_agent_state
    ):
        """Test complete processing flow."""
        result = await processor.process(
            sample_unstructured_logs, "test_data_id", sample_agent_state
        )

        assert isinstance(result, DataInsightsResponse)
        assert result.data_id == "test_data_id"
        assert result.data_type == DataType.LOG_FILE
        assert result.confidence_score >= 0.0

    @pytest.mark.asyncio
    async def test_process_with_anomalies(self, processor, sample_agent_state):
        """Test processing with anomaly detection."""
        # Create logs with potential anomalies
        anomaly_logs = """
2024-01-01 12:00:00 INFO Normal message
2024-01-01 12:00:01 INFO Normal message
2024-01-01 12:00:02 ERROR Anomaly detected
2024-01-01 12:00:03 INFO Normal message
"""

        result = await processor.process(
            anomaly_logs, "test_data_id", sample_agent_state
        )

        assert isinstance(result, DataInsightsResponse)
        assert result.data_id == "test_data_id"
        assert isinstance(result.anomalies_detected, list)

    @pytest.mark.asyncio
    async def test_process_empty_input(self, processor, sample_agent_state):
        """Test processing with empty input."""
        result = await processor.process("", "test_data_id", sample_agent_state)

        assert isinstance(result, DataInsightsResponse)
        assert result.data_id == "test_data_id"
        assert result.confidence_score == 0.0

    @pytest.mark.asyncio
    async def test_process_large_dataset(self, processor, sample_agent_state):
        """Test processing with a large dataset."""
        large_logs = "\n".join(
            [f"2024-01-01 12:00:{i:02d} INFO Message {i}" for i in range(100)]
        )

        result = await processor.process(large_logs, "test_data_id", sample_agent_state)

        assert isinstance(result, DataInsightsResponse)
        assert result.data_id == "test_data_id"
        assert result.confidence_score >= 0.0

    @pytest.mark.asyncio
    async def test_process_with_special_characters(self, processor, sample_agent_state):
        """Test processing with special characters in logs."""
        special_logs = """
2024-01-01 12:00:00 INFO Special chars: àáâãäåæçèé
2024-01-01 12:00:01 ERROR Unicode: 你好世界
2024-01-01 12:00:02 WARN Symbols: !@#$%^&*()
"""

        result = await processor.process(
            special_logs, "test_data_id", sample_agent_state
        )

        assert isinstance(result, DataInsightsResponse)
        assert result.data_id == "test_data_id"
        assert result.confidence_score >= 0.0

    def test_anomaly_detection_with_insufficient_data(
        self, processor, sample_agent_state
    ):
        """Test anomaly detection with insufficient data."""
        # Create minimal data
        minimal_logs = "2024-01-01 12:00:00 INFO Single message"

        df = processor._parse_logs_to_dataframe(minimal_logs)
        anomalies = processor._detect_anomalies(df)

        # Should handle insufficient data gracefully
        assert isinstance(anomalies, list)
        # With only one log entry, no anomalies should be detected
        assert len(anomalies) == 0
