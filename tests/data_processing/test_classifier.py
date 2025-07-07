from unittest.mock import AsyncMock, Mock

import pytest

from faultmaven.data_processing.classifier import DataClassifier
from faultmaven.models import DataType


class TestDataClassifier:
    """Test suite for DataClassifier class."""

    @pytest.fixture
    def mock_router(self):
        """Mock LLMRouter dependency."""
        return Mock()

    @pytest.fixture
    def classifier(self, mock_router):
        """Create DataClassifier instance with mocked router."""
        classifier = DataClassifier()
        classifier.llm_router = mock_router
        return classifier

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text,expected_type",
        [
            (
                "2024-01-01 12:00:00 ERROR [app] Database connection failed",
                DataType.LOG_FILE,
            ),
            ("[ERROR] Failed to connect to database", DataType.ERROR_MESSAGE),
            ("INFO: Application started successfully", DataType.LOG_FILE),
            ("DEBUG: Processing request ID 12345", DataType.LOG_FILE),
            ("Exception: java.lang.NullPointerException", DataType.ERROR_MESSAGE),
            ("Error: Division by zero", DataType.ERROR_MESSAGE),
            ("Stack trace: at com.example.App.main", DataType.CONFIG_FILE),
            ("cpu_usage{host='server1'} 85.2", DataType.METRICS_DATA),
            ("memory_usage_percent 67.8", DataType.CONFIG_FILE),
            ("http_requests_total{method='GET'} 1234", DataType.METRICS_DATA),
            ("database.host=localhost", DataType.CONFIG_FILE),
            ("api.timeout=30", DataType.CONFIG_FILE),
            ("logging.level=DEBUG", DataType.LOG_FILE),
            ("This is a troubleshooting guide.", DataType.DOCUMENTATION),
            ("Some random text that doesn't match patterns", DataType.CONFIG_FILE),
        ],
    )
    async def test_heuristic_classification(self, classifier, text, expected_type):
        """Test heuristic-based classification for various data types."""
        result = await classifier.classify(text)
        assert result == expected_type

    @pytest.mark.asyncio
    async def test_llm_fallback_ambiguous_text(self, classifier, mock_router):
        """Test LLM fallback for ambiguous text."""
        mock_response = Mock()
        mock_response.content = "log_file"
        mock_router.route = AsyncMock(return_value=mock_response)
        classifier._heuristic_classify = lambda _: DataType.UNKNOWN
        ambiguous_text = "foobar123"
        result = await classifier.classify(ambiguous_text)
        mock_router.route.assert_awaited_once()
        assert result == DataType.LOG_FILE

    @pytest.mark.asyncio
    async def test_llm_fallback_returns_valid_type(self, classifier, mock_router):
        """Test that LLM fallback returns a valid DataType."""
        mock_response = Mock()
        mock_response.content = "metrics_data"
        mock_router.route = AsyncMock(return_value=mock_response)
        classifier._heuristic_classify = lambda _: DataType.UNKNOWN
        ambiguous_text = "foobar123"
        result = await classifier.classify(ambiguous_text)
        assert result == DataType.METRICS_DATA

    @pytest.mark.asyncio
    async def test_llm_fallback_invalid_response(self, classifier, mock_router):
        """Test handling of invalid LLM response."""
        mock_response = Mock()
        mock_response.content = "INVALID_TYPE"
        mock_router.route = AsyncMock(return_value=mock_response)
        classifier._heuristic_classify = lambda _: DataType.UNKNOWN
        ambiguous_text = "foobar123"
        result = await classifier.classify(ambiguous_text)
        assert result == DataType.UNKNOWN

    @pytest.mark.asyncio
    async def test_llm_fallback_exception_handling(self, classifier, mock_router):
        """Test handling of LLM router exceptions."""

        async def raise_exc(*args, **kwargs):
            raise Exception("LLM error")

        mock_router.route = AsyncMock(side_effect=raise_exc)
        classifier._heuristic_classify = lambda _: DataType.UNKNOWN
        ambiguous_text = "foobar123"
        result = await classifier.classify(ambiguous_text)
        assert result == DataType.UNKNOWN

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("", DataType.UNKNOWN),
            (None, DataType.UNKNOWN),
            ("   ", DataType.CONFIG_FILE),
            ("\n\t", DataType.UNKNOWN),
        ],
    )
    async def test_empty_or_whitespace_input(self, classifier, text, expected):
        """Test handling of empty or whitespace-only input."""
        result = await classifier.classify(text)
        assert result == expected

    @pytest.mark.asyncio
    async def test_case_insensitive_classification(self, classifier):
        """Test that classification works regardless of case."""
        mixed_case_text = "ERROR: Database connection FAILED"
        result = await classifier.classify(mixed_case_text)
        assert result == DataType.ERROR_MESSAGE

    @pytest.mark.asyncio
    async def test_multiple_patterns_in_text(self, classifier):
        """Test classification when multiple patterns are present."""
        mixed_text = "ERROR: CPU usage is 95%"
        result = await classifier.classify(mixed_text)
        assert result == DataType.ERROR_MESSAGE

    @pytest.mark.asyncio
    async def test_heuristic_priority_order(self, classifier):
        """Test that heuristic patterns are checked in priority order."""
        text = "ERROR: Some error message"
        result = await classifier.classify(text)
        assert result == DataType.ERROR_MESSAGE

    @pytest.mark.asyncio
    async def test_llm_router_called_only_once(self, classifier, mock_router):
        """Test that LLM router is called exactly once for ambiguous text."""
        mock_response = Mock()
        mock_response.content = "documentation"
        mock_router.route = AsyncMock(return_value=mock_response)
        classifier._heuristic_classify = lambda _: DataType.UNKNOWN
        ambiguous_text = "foobar123"
        await classifier.classify(ambiguous_text)
        assert mock_router.route.await_count == 1

    @pytest.mark.asyncio
    async def test_classification_with_special_characters(self, classifier):
        """Test classification with special characters and symbols."""
        special_text = "ERROR: @#$%^&*() connection failed!"
        result = await classifier.classify(special_text)
        assert result == DataType.ERROR_MESSAGE

    @pytest.mark.asyncio
    async def test_classification_with_unicode(self, classifier):
        """Test classification with unicode characters."""
        unicode_text = "ERROR: Database connection failed 🚫"
        result = await classifier.classify(unicode_text)
        assert result == DataType.ERROR_MESSAGE
