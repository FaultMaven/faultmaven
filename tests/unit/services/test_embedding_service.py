"""Unit tests for EmbeddingService.

Tests cover:
- Embedding generation success
- Batch embedding generation
- Input validation (empty text, too long text)
- Retry logic on transient failures
- Rate limit handling (429 error)
- API error handling
- Token usage tracking
"""

from typing import List
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from faultmaven.exceptions import EmbeddingInvalidInputError

import pytest

from faultmaven.exceptions import (
    EmbeddingGenerationError,
    EmbeddingInvalidInputError,
    EmbeddingRateLimitError,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    EMBEDDING_DIMENSIONS,
)
from faultmaven.modules.knowledge.domain.services.embedding_service import (
    EmbeddingService,
)


# Test fixtures
@pytest.fixture
def mock_openai_client():
    """Create mock OpenAI client."""
    with patch(
        "faultmaven.modules.knowledge.domain.services.embedding_service.AsyncOpenAI"
    ) as mock:
        client = MagicMock()
        mock.return_value = client
        yield client


@pytest.fixture
def embedding_service(mock_openai_client):
    """Create embedding service with mocked client."""
    service = EmbeddingService(
        api_key="test-api-key",
        model="text-embedding-3-small",
        dimensions=EMBEDDING_DIMENSIONS,
        max_retries=3,
        retry_delay=0.01,  # Fast retries for testing
        timeout=60,
        max_text_length=8191,
    )
    service.client = mock_openai_client
    return service


def create_mock_embedding(
    dimensions: int = EMBEDDING_DIMENSIONS, value: float = 0.1
) -> List[float]:
    """Create mock embedding vector."""
    return [value] * dimensions


def create_mock_response(embedding: List[float], total_tokens: int = 100):
    """Create mock OpenAI embedding response."""
    response = MagicMock()
    response.data = [MagicMock(embedding=embedding, index=0)]
    response.usage = MagicMock(total_tokens=total_tokens)
    return response


def create_mock_batch_response(embeddings: List[List[float]], total_tokens: int = 100):
    """Create mock OpenAI batch embedding response."""
    response = MagicMock()
    response.data = [
        MagicMock(embedding=emb, index=i) for i, emb in enumerate(embeddings)
    ]
    response.usage = MagicMock(total_tokens=total_tokens)
    return response


# =============================================================================
# Test: generate_embedding() - Success Cases
# =============================================================================


class TestGenerateEmbeddingSuccess:
    """Tests for successful embedding generation."""

    @pytest.mark.asyncio
    async def test_generate_embedding_success(
        self, embedding_service, mock_openai_client
    ):
        """Test successful single embedding generation."""
        mock_embedding = create_mock_embedding()
        mock_openai_client.embeddings.create = AsyncMock(
            return_value=create_mock_response(mock_embedding)
        )

        result = await embedding_service.generate_embedding("Hello, world!")

        assert result == mock_embedding
        assert len(result) == EMBEDDING_DIMENSIONS
        mock_openai_client.embeddings.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_embedding_tracks_tokens(
        self, embedding_service, mock_openai_client
    ):
        """Test that token usage is tracked."""
        mock_embedding = create_mock_embedding()
        mock_openai_client.embeddings.create = AsyncMock(
            return_value=create_mock_response(mock_embedding, total_tokens=150)
        )

        await embedding_service.generate_embedding("Test text")

        assert embedding_service.get_total_tokens() == 150

    @pytest.mark.asyncio
    async def test_generate_embedding_with_unicode(
        self, embedding_service, mock_openai_client
    ):
        """Test embedding generation with unicode text."""
        mock_embedding = create_mock_embedding()
        mock_openai_client.embeddings.create = AsyncMock(
            return_value=create_mock_response(mock_embedding)
        )

        result = await embedding_service.generate_embedding("日本語テスト 中文测试 🎉")

        assert len(result) == EMBEDDING_DIMENSIONS

    @pytest.mark.asyncio
    async def test_generate_embedding_with_long_text(
        self, embedding_service, mock_openai_client
    ):
        """Test embedding generation with long text (under limit)."""
        mock_embedding = create_mock_embedding()
        mock_openai_client.embeddings.create = AsyncMock(
            return_value=create_mock_response(mock_embedding)
        )

        long_text = "a" * 5000  # Under 8191 limit
        result = await embedding_service.generate_embedding(long_text)

        assert len(result) == EMBEDDING_DIMENSIONS

    @pytest.mark.asyncio
    async def test_generate_embedding_multiple_calls(
        self, embedding_service, mock_openai_client
    ):
        """Test multiple embedding generations accumulate tokens."""
        mock_embedding = create_mock_embedding()
        mock_openai_client.embeddings.create = AsyncMock(
            return_value=create_mock_response(mock_embedding, total_tokens=100)
        )

        await embedding_service.generate_embedding("Text 1")
        await embedding_service.generate_embedding("Text 2")
        await embedding_service.generate_embedding("Text 3")

        assert embedding_service.get_total_tokens() == 300


# =============================================================================
# Test: generate_embedding() - Validation Errors
# =============================================================================


class TestGenerateEmbeddingValidation:
    """Tests for input validation in embedding generation."""

    @pytest.mark.asyncio
    async def test_generate_embedding_empty_text_raises_error(self, embedding_service):
        """Test that empty text raises EmbeddingInvalidInputError."""
        with pytest.raises(EmbeddingInvalidInputError) as exc_info:
            await embedding_service.generate_embedding("")

        assert "cannot be empty" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_generate_embedding_whitespace_only_raises_error(
        self, embedding_service
    ):
        """Test that whitespace-only text raises error."""
        with pytest.raises(EmbeddingInvalidInputError) as exc_info:
            await embedding_service.generate_embedding("   \n\t  ")

        assert "whitespace" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_generate_embedding_too_long_raises_error(self, embedding_service):
        """Test that text exceeding max length raises error."""
        from faultmaven.exceptions import EmbeddingInvalidInputError

        long_text = "a" * 10000  # Exceeds 8191 limit

        with pytest.raises(EmbeddingInvalidInputError) as exc_info:
            await embedding_service.generate_embedding(long_text)

        assert "exceeds maximum length" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_generate_embedding_none_raises_error(self, embedding_service):
        """Test that None raises RuntimeError (wrapped validation error)."""
        with pytest.raises(EmbeddingInvalidInputError):
            await embedding_service.generate_embedding(None)

    @pytest.mark.asyncio
    async def test_generate_embedding_non_string_raises_error(self, embedding_service):
        """Test that non-string input raises error."""
        with pytest.raises(EmbeddingInvalidInputError) as exc_info:
            await embedding_service.generate_embedding(12345)

        assert "must be a string" in str(exc_info.value).lower()


# =============================================================================
# Test: generate_embedding() - Retry Logic
# =============================================================================


class TestGenerateEmbeddingRetry:
    """Tests for retry logic on transient failures."""

    @pytest.mark.asyncio
    async def test_retry_on_connection_error(
        self, embedding_service, mock_openai_client
    ):
        """Test retry on APIConnectionError."""
        from openai import APIConnectionError

        mock_embedding = create_mock_embedding()
        mock_openai_client.embeddings.create = AsyncMock(
            side_effect=[
                APIConnectionError(request=MagicMock()),
                APIConnectionError(request=MagicMock()),
                create_mock_response(mock_embedding),
            ]
        )

        result = await embedding_service.generate_embedding("Test")

        assert result == mock_embedding
        assert mock_openai_client.embeddings.create.call_count == 3

    @pytest.mark.asyncio
    async def test_retry_on_timeout_error(self, embedding_service, mock_openai_client):
        """Test retry on APITimeoutError."""
        from openai import APITimeoutError

        mock_embedding = create_mock_embedding()
        mock_openai_client.embeddings.create = AsyncMock(
            side_effect=[
                APITimeoutError(request=MagicMock()),
                create_mock_response(mock_embedding),
            ]
        )

        result = await embedding_service.generate_embedding("Test")

        assert result == mock_embedding
        assert mock_openai_client.embeddings.create.call_count == 2

    @pytest.mark.asyncio
    async def test_max_retries_exceeded_raises_error(
        self, embedding_service, mock_openai_client
    ):
        """Test that exceeding max retries raises EmbeddingGenerationError."""
        from openai import APIConnectionError

        mock_openai_client.embeddings.create = AsyncMock(
            side_effect=APIConnectionError(request=MagicMock())
        )

        with pytest.raises(EmbeddingGenerationError) as exc_info:
            await embedding_service.generate_embedding("Test")

        assert "retries" in str(exc_info.value).lower()
        assert mock_openai_client.embeddings.create.call_count == 3


# =============================================================================
# Test: generate_embedding() - Rate Limit Handling
# =============================================================================


class TestGenerateEmbeddingRateLimit:
    """Tests for rate limit handling."""

    @pytest.mark.asyncio
    async def test_rate_limit_retry_then_success(
        self, embedding_service, mock_openai_client
    ):
        """Test retry on rate limit then success."""
        from openai import RateLimitError

        mock_embedding = create_mock_embedding()
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}

        mock_openai_client.embeddings.create = AsyncMock(
            side_effect=[
                RateLimitError(
                    message="Rate limit exceeded",
                    response=mock_response,
                    body=None,
                ),
                create_mock_response(mock_embedding),
            ]
        )

        result = await embedding_service.generate_embedding("Test")

        assert result == mock_embedding
        assert mock_openai_client.embeddings.create.call_count == 2

    @pytest.mark.asyncio
    async def test_rate_limit_max_retries_raises_error(
        self, embedding_service, mock_openai_client
    ):
        """Test that rate limit after max retries raises EmbeddingRateLimitError."""
        from openai import RateLimitError

        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}

        mock_openai_client.embeddings.create = AsyncMock(
            side_effect=RateLimitError(
                message="Rate limit exceeded",
                response=mock_response,
                body=None,
            )
        )

        with pytest.raises(EmbeddingRateLimitError) as exc_info:
            await embedding_service.generate_embedding("Test")

        assert "rate limit" in str(exc_info.value).lower()


# =============================================================================
# Test: generate_embedding() - API Errors
# =============================================================================


class TestGenerateEmbeddingAPIErrors:
    """Tests for API error handling."""

    @pytest.mark.asyncio
    async def test_api_error_raises_embedding_generation_error(
        self, embedding_service, mock_openai_client
    ):
        """Test that APIError raises EmbeddingGenerationError."""
        from openai import APIError

        mock_response = MagicMock()
        mock_response.status_code = 400

        mock_openai_client.embeddings.create = AsyncMock(
            side_effect=APIError(
                message="Bad request",
                request=MagicMock(),
                body=None,
            )
        )

        with pytest.raises(EmbeddingGenerationError) as exc_info:
            await embedding_service.generate_embedding("Test")

        assert "api error" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_unexpected_error_raises_embedding_generation_error(
        self, embedding_service, mock_openai_client
    ):
        """Test that unexpected errors raise EmbeddingGenerationError."""
        mock_openai_client.embeddings.create = AsyncMock(
            side_effect=Exception("Unexpected error")
        )

        with pytest.raises(EmbeddingGenerationError) as exc_info:
            await embedding_service.generate_embedding("Test")

        assert "unexpected" in str(exc_info.value).lower()


# =============================================================================
# Test: generate_embeddings_batch() - Success Cases
# =============================================================================


class TestGenerateEmbeddingsBatchSuccess:
    """Tests for successful batch embedding generation."""

    @pytest.mark.asyncio
    async def test_generate_embeddings_batch_success(
        self, embedding_service, mock_openai_client
    ):
        """Test successful batch embedding generation."""
        texts = ["Text 1", "Text 2", "Text 3"]
        embeddings = [create_mock_embedding(value=0.1 * i) for i in range(len(texts))]

        mock_openai_client.embeddings.create = AsyncMock(
            return_value=create_mock_batch_response(embeddings)
        )

        result = await embedding_service.generate_embeddings_batch(texts)

        assert len(result) == len(texts)
        for emb in result:
            assert len(emb) == EMBEDDING_DIMENSIONS

    @pytest.mark.asyncio
    async def test_generate_embeddings_batch_empty_list(self, embedding_service):
        """Test batch with empty list returns empty list."""
        result = await embedding_service.generate_embeddings_batch([])

        assert result == []

    @pytest.mark.asyncio
    async def test_generate_embeddings_batch_single_item(
        self, embedding_service, mock_openai_client
    ):
        """Test batch with single item."""
        texts = ["Single text"]
        embeddings = [create_mock_embedding()]

        mock_openai_client.embeddings.create = AsyncMock(
            return_value=create_mock_batch_response(embeddings)
        )

        result = await embedding_service.generate_embeddings_batch(texts)

        assert len(result) == 1
        assert len(result[0]) == EMBEDDING_DIMENSIONS

    @pytest.mark.asyncio
    async def test_generate_embeddings_batch_multiple_batches(
        self, embedding_service, mock_openai_client
    ):
        """Test processing in multiple batches."""
        texts = ["Text " + str(i) for i in range(150)]  # More than batch_size=100

        # Mock will be called twice (100 + 50)
        batch1_embeddings = [create_mock_embedding() for _ in range(100)]
        batch2_embeddings = [create_mock_embedding() for _ in range(50)]

        mock_openai_client.embeddings.create = AsyncMock(
            side_effect=[
                create_mock_batch_response(batch1_embeddings),
                create_mock_batch_response(batch2_embeddings),
            ]
        )

        result = await embedding_service.generate_embeddings_batch(texts)

        assert len(result) == 150
        assert mock_openai_client.embeddings.create.call_count == 2


# =============================================================================
# Test: generate_embeddings_batch() - Validation Errors
# =============================================================================


class TestGenerateEmbeddingsBatchValidation:
    """Tests for batch input validation."""

    @pytest.mark.asyncio
    async def test_batch_with_empty_text_raises_error(self, embedding_service):
        """Test that empty text in batch raises error."""
        texts = ["Valid text", "", "Another valid text"]

        with pytest.raises(EmbeddingInvalidInputError) as exc_info:
            await embedding_service.generate_embeddings_batch(texts)

        assert "index 1" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_batch_with_too_long_text_raises_error(self, embedding_service):
        """Test that too long text in batch raises error."""
        texts = ["Valid", "a" * 10000, "Valid"]

        with pytest.raises(EmbeddingInvalidInputError):
            await embedding_service.generate_embeddings_batch(texts)

    @pytest.mark.asyncio
    async def test_batch_with_non_list_raises_error(self, embedding_service):
        """Test that non-list input raises error."""
        with pytest.raises(EmbeddingInvalidInputError):
            await embedding_service.generate_embeddings_batch("not a list")


# =============================================================================
# Test: Token Tracking
# =============================================================================


class TestTokenTracking:
    """Tests for token usage tracking."""

    @pytest.mark.asyncio
    async def test_get_total_tokens_initial(self, embedding_service):
        """Test initial token count is zero."""
        assert embedding_service.get_total_tokens() == 0

    @pytest.mark.asyncio
    async def test_reset_token_count(self, embedding_service, mock_openai_client):
        """Test resetting token count."""
        mock_embedding = create_mock_embedding()
        mock_openai_client.embeddings.create = AsyncMock(
            return_value=create_mock_response(mock_embedding, total_tokens=100)
        )

        await embedding_service.generate_embedding("Test")
        assert embedding_service.get_total_tokens() == 100

        embedding_service.reset_token_count()
        assert embedding_service.get_total_tokens() == 0


# =============================================================================
# Test: Service Stats
# =============================================================================


class TestServiceStats:
    """Tests for service statistics."""

    def test_get_stats(self, embedding_service):
        """Test getting service statistics."""
        stats = embedding_service.get_stats()

        assert stats["model"] == "text-embedding-3-small"
        assert stats["dimensions"] == EMBEDDING_DIMENSIONS
        assert stats["total_tokens"] == 0
        assert stats["max_retries"] == 3

    @pytest.mark.asyncio
    async def test_get_stats_after_usage(self, embedding_service, mock_openai_client):
        """Test stats after generating embeddings."""
        mock_embedding = create_mock_embedding()
        mock_openai_client.embeddings.create = AsyncMock(
            return_value=create_mock_response(mock_embedding, total_tokens=250)
        )

        await embedding_service.generate_embedding("Test")

        stats = embedding_service.get_stats()
        assert stats["total_tokens"] == 250


# =============================================================================
# Test: Health Check
# =============================================================================


class TestHealthCheck:
    """Tests for health check functionality."""

    @pytest.mark.asyncio
    async def test_health_check_healthy(self, embedding_service, mock_openai_client):
        """Test health check returns healthy when API works."""
        mock_embedding = create_mock_embedding()
        mock_openai_client.embeddings.create = AsyncMock(
            return_value=create_mock_response(mock_embedding)
        )

        health = await embedding_service.health_check()

        assert health["status"] == "healthy"
        assert health["api_status"] == "healthy"
        assert health["model"] == "text-embedding-3-small"

    @pytest.mark.asyncio
    async def test_health_check_unhealthy(self, embedding_service, mock_openai_client):
        """Test health check returns unhealthy when API fails."""
        mock_openai_client.embeddings.create = AsyncMock(
            side_effect=Exception("API failure")
        )

        health = await embedding_service.health_check()

        assert health["api_status"] == "unhealthy"
        assert "error" in health
