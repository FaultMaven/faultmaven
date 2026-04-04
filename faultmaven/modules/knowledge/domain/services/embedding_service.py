"""Embedding Service for generating text embeddings.

This service provides text embedding generation capabilities for the
RAG (Retrieval-Augmented Generation) system. The canonical model is BGE-M3
(1024 dims) loaded via model_cache. This service is part of the
KnowledgeSearchService path which is currently superseded — see
vector-retrieval-architecture.md §7 for details.

Features:
- Single text embedding generation
- Batch embedding generation with configurable batch sizes
- Retry logic with exponential backoff for transient failures
- Rate limit handling (429 status)
- Token usage tracking for cost monitoring
- Input validation (empty text, too long text)

Usage:
    from faultmaven.modules.knowledge.domain.services.embedding_service import EmbeddingService

    service = EmbeddingService(api_key="sk-...")
    embedding = await service.generate_embedding("Hello, world!")
    embeddings = await service.generate_embeddings_batch(["text1", "text2"])
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)

from faultmaven.exceptions import (
    EmbeddingGenerationError,
    EmbeddingInvalidInputError,
    EmbeddingRateLimitError,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    EMBEDDING_DIMENSIONS,
)

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Service for generating text embeddings using OpenAI.

    This service handles embedding generation with:
    - Retry logic for transient API failures
    - Rate limit handling with exponential backoff
    - Input validation for text length and content
    - Token usage tracking for cost monitoring

    Attributes:
        client: AsyncOpenAI client for API calls
        model: Embedding model name (default: bge-m3)
        dimensions: Vector dimensions (default: 1024)
        max_retries: Maximum retry attempts
        retry_delay: Base delay between retries
        max_text_length: Maximum text length for embedding
        _total_tokens: Total tokens used across all calls
    """

    def __init__(
        self,
        api_key: str,
        model: str = "bge-m3",
        dimensions: int = EMBEDDING_DIMENSIONS,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        timeout: int = 60,
        max_text_length: int = 8191,
    ):
        """Initialize embedding service.

        Args:
            api_key: OpenAI API key
            model: Embedding model name
            dimensions: Vector dimensions (1024 for bge-m3)
            max_retries: Maximum retry attempts for API calls
            retry_delay: Base delay between retries (exponential backoff)
            timeout: Timeout for API calls in seconds
            max_text_length: Maximum text length for embedding
        """
        self.client = AsyncOpenAI(api_key=api_key, timeout=timeout)
        self.model = model
        self.dimensions = dimensions
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.timeout = timeout
        self.max_text_length = max_text_length
        self._total_tokens = 0

    async def generate_embedding(self, text: str) -> List[float]:
        """Generate embedding for text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector (1024 dimensions, BGE-M3)

        Raises:
            EmbeddingInvalidInputError: If text is empty or too long
            EmbeddingRateLimitError: If API rate limit is exceeded
            EmbeddingGenerationError: If embedding generation fails
        """
        self._validate_text(text)
        return await self._generate_embedding_impl(text)

    async def _generate_embedding_impl(self, text: str) -> List[float]:
        """Internal implementation of embedding generation with retries."""
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                response = await self.client.embeddings.create(
                    model=self.model,
                    input=text,
                    dimensions=self.dimensions,
                )

                # Track token usage
                if hasattr(response, "usage") and response.usage:
                    self._total_tokens += response.usage.total_tokens

                embedding = response.data[0].embedding

                logger.info("Metric: embedding_generated")

                return embedding

            except RateLimitError as e:
                last_error = e
                wait_time = self.retry_delay * (2**attempt)
                logger.warning(
                    f"Rate limit hit, waiting {wait_time}s before retry "
                    f"(attempt {attempt + 1}/{self.max_retries})"
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(wait_time)
                else:
                    raise EmbeddingRateLimitError(
                        f"Rate limit exceeded after {self.max_retries} retries",
                        details={"model": self.model, "retries": self.max_retries},
                    ) from e

            except (APIConnectionError, APITimeoutError) as e:
                last_error = e
                wait_time = self.retry_delay * (2**attempt)
                logger.warning(
                    f"Transient API error: {e}, waiting {wait_time}s before retry "
                    f"(attempt {attempt + 1}/{self.max_retries})"
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(wait_time)
                else:
                    raise EmbeddingGenerationError(
                        f"Connection/timeout error after {self.max_retries} retries: {e}",
                        details={
                            "model": self.model,
                            "retries": self.max_retries,
                            "error_type": type(e).__name__,
                        },
                    ) from e

            except APIError as e:
                # Non-retriable API error
                logger.error(f"API error generating embedding: {e}")
                raise EmbeddingGenerationError(
                    f"API error generating embedding: {e}",
                    details={
                        "model": self.model,
                        "status_code": getattr(e, "status_code", None),
                        "error_type": type(e).__name__,
                    },
                ) from e

            except Exception as e:
                # Unexpected error
                logger.error(f"Unexpected error generating embedding: {e}")
                raise EmbeddingGenerationError(
                    f"Unexpected error generating embedding: {e}",
                    details={
                        "model": self.model,
                        "error_type": type(e).__name__,
                    },
                ) from e

        # Should not reach here, but just in case
        raise EmbeddingGenerationError(
            f"Failed to generate embedding after {self.max_retries} retries",
            details={"last_error": str(last_error)},
        )

    async def generate_embeddings_batch(
        self,
        texts: List[str],
        batch_size: int = 100,
    ) -> List[List[float]]:
        """Generate embeddings for multiple texts in batches.

        Args:
            texts: List of texts to embed
            batch_size: Number of texts per API call (default: 100)

        Returns:
            List of embedding vectors

        Raises:
            EmbeddingInvalidInputError: If any text is invalid
            EmbeddingRateLimitError: If API rate limit is exceeded
            EmbeddingGenerationError: If embedding generation fails
        """
        self._validate_texts(texts)
        return await self._generate_embeddings_batch_impl(texts, batch_size)

    async def _generate_embeddings_batch_impl(
        self,
        texts: List[str],
        batch_size: int,
    ) -> List[List[float]]:
        """Internal implementation of batch embedding generation."""
        if not texts:
            return []

        all_embeddings: List[List[float]] = []

        # Process in batches
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            batch_embeddings = await self._process_batch(batch)
            all_embeddings.extend(batch_embeddings)

            logger.info("Metric: embedding_batch_processed")

        return all_embeddings

    async def _process_batch(self, batch: List[str]) -> List[List[float]]:
        """Process a single batch of texts."""
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                response = await self.client.embeddings.create(
                    model=self.model,
                    input=batch,
                    dimensions=self.dimensions,
                )

                # Track token usage
                if hasattr(response, "usage") and response.usage:
                    self._total_tokens += response.usage.total_tokens

                # Sort by index to maintain order
                embeddings = [None] * len(batch)
                for item in response.data:
                    embeddings[item.index] = item.embedding

                return embeddings

            except RateLimitError as e:
                last_error = e
                wait_time = self.retry_delay * (2**attempt)
                logger.warning(
                    f"Rate limit hit on batch, waiting {wait_time}s before retry "
                    f"(attempt {attempt + 1}/{self.max_retries})"
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(wait_time)
                else:
                    raise EmbeddingRateLimitError(
                        f"Rate limit exceeded on batch after {self.max_retries} retries",
                        details={
                            "model": self.model,
                            "batch_size": len(batch),
                            "retries": self.max_retries,
                        },
                    ) from e

            except (APIConnectionError, APITimeoutError) as e:
                last_error = e
                wait_time = self.retry_delay * (2**attempt)
                logger.warning(
                    f"Transient API error on batch: {e}, waiting {wait_time}s "
                    f"(attempt {attempt + 1}/{self.max_retries})"
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(wait_time)
                else:
                    raise EmbeddingGenerationError(
                        f"Connection/timeout error on batch after {self.max_retries} retries: {e}",
                        details={
                            "model": self.model,
                            "batch_size": len(batch),
                            "retries": self.max_retries,
                            "error_type": type(e).__name__,
                        },
                    ) from e

            except APIError as e:
                logger.error(f"API error generating batch embeddings: {e}")
                raise EmbeddingGenerationError(
                    f"API error generating batch embeddings: {e}",
                    details={
                        "model": self.model,
                        "batch_size": len(batch),
                        "status_code": getattr(e, "status_code", None),
                        "error_type": type(e).__name__,
                    },
                ) from e

            except Exception as e:
                logger.error(f"Unexpected error generating batch embeddings: {e}")
                raise EmbeddingGenerationError(
                    f"Unexpected error generating batch embeddings: {e}",
                    details={
                        "model": self.model,
                        "batch_size": len(batch),
                        "error_type": type(e).__name__,
                    },
                ) from e

        raise EmbeddingGenerationError(
            f"Failed to generate batch embeddings after {self.max_retries} retries",
            details={"last_error": str(last_error)},
        )

    def _validate_text(self, text: str) -> None:
        """Validate a single text for embedding.

        Args:
            text: Text to validate

        Raises:
            EmbeddingInvalidInputError: If text is invalid
        """
        if not text:
            raise EmbeddingInvalidInputError(
                "Text cannot be empty",
                details={"text_length": 0},
            )

        if not isinstance(text, str):
            raise EmbeddingInvalidInputError(
                f"Text must be a string, got {type(text).__name__}",
                details={"type": type(text).__name__},
            )

        # Strip and check again
        text = text.strip()
        if not text:
            raise EmbeddingInvalidInputError(
                "Text cannot be whitespace only",
                details={"text_length": 0},
            )

        if len(text) > self.max_text_length:
            raise EmbeddingInvalidInputError(
                f"Text exceeds maximum length of {self.max_text_length} characters",
                details={
                    "text_length": len(text),
                    "max_length": self.max_text_length,
                },
            )

    def _validate_texts(self, texts: List[str]) -> None:
        """Validate a list of texts for batch embedding.

        Args:
            texts: List of texts to validate

        Raises:
            EmbeddingInvalidInputError: If any text is invalid
        """
        if not isinstance(texts, list):
            raise EmbeddingInvalidInputError(
                f"Texts must be a list, got {type(texts).__name__}",
                details={"type": type(texts).__name__},
            )

        for i, text in enumerate(texts):
            try:
                self._validate_text(text)
            except EmbeddingInvalidInputError as e:
                raise EmbeddingInvalidInputError(
                    f"Invalid text at index {i}: {e}",
                    details={**e.details, "index": i},
                ) from e

    def get_total_tokens(self) -> int:
        """Get total tokens used across all API calls.

        Returns:
            Total token count for cost monitoring
        """
        return self._total_tokens

    def reset_token_count(self) -> None:
        """Reset the token counter to zero."""
        self._total_tokens = 0

    def get_stats(self) -> Dict[str, Any]:
        """Get service statistics.

        Returns:
            Dictionary with service statistics
        """
        return {
            "model": self.model,
            "dimensions": self.dimensions,
            "total_tokens": self._total_tokens,
            "max_retries": self.max_retries,
            "retry_delay": self.retry_delay,
        }

    async def health_check(self) -> Dict[str, Any]:
        """Perform health check for the embedding service.

        Returns:
            Dictionary with health status
        """
        base_health = {"service": "embedding_service", "status": "healthy"}

        try:
            # Try generating a simple embedding to verify API connectivity
            test_embedding = await self._generate_embedding_impl("health check")
            api_status = (
                "healthy" if len(test_embedding) == self.dimensions else "degraded"
            )
        except Exception as e:
            api_status = "unhealthy"
            base_health["error"] = str(e)

        base_health.update(
            {
                "api_status": api_status,
                "model": self.model,
                "dimensions": self.dimensions,
                "total_tokens_used": self._total_tokens,
            }
        )

        return base_health
