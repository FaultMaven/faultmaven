"""
Semantic cache for LLM responses.

This module provides semantic caching functionality for LLM responses
to reduce API calls and improve response times.
"""

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import numpy as np

from ..model_cache import model_cache
from .providers import LLMResponse


class SemanticCache:
    """Semantic cache for LLM responses"""

    def __init__(self, similarity_threshold: float = 0.85, max_size: int = 1000):
        self.similarity_threshold = similarity_threshold
        self.max_size = max_size
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.embeddings: Dict[str, np.ndarray] = {}
        self.logger = logging.getLogger(__name__)

    @property
    def encoder(self) -> Optional[Any]:
        """BGE-M3 for semantic similarity — only if it is ALREADY loaded.

        Resolved per use through ``peek_bge_m3_model`` rather than bound in
        ``__init__``, because this cache is constructed by ``LLMRouter``, which
        the DI container builds in *every* process — including cleanup
        CronJobs, which were OOMKilled loading a 1.3Gi model they never use
        (#868). Semantic similarity is an optimisation, so it never justifies
        forcing the load: it is available exactly when the deployment's
        configured policy (``PRELOAD_MODELS``, applied in the web lifespan) or
        a prior embed has already paid for the model.

        Peeking rather than loading also keeps a 60–120s cold load off the
        request path, where it would block the event loop and trip liveness.
        When the model is absent the cache degrades to exact-key matching —
        the documented fallback, correct but with fewer hits.
        """
        return model_cache.peek_bge_m3_model()

    def _get_cache_key(
        self, prompt: str, model: str, case_id: Optional[str] = None
    ) -> str:
        """Generate cache key scoped to case, prompt, and model"""
        content = f"{case_id or ''}:{prompt}:{model}"
        return hashlib.sha256(content.encode()).hexdigest()

    def _compute_embedding(self, text: str) -> Optional[np.ndarray]:
        """Compute embedding for text"""
        if not self.encoder:
            return None
        try:
            return self.encoder.encode([text])[0]
        except Exception as e:
            self.logger.warning(f"Failed to compute embedding: {e}")
            return None

    def _compute_similarity(
        self, embedding1: np.ndarray, embedding2: np.ndarray
    ) -> float:
        """Compute cosine similarity between embeddings"""
        try:
            return np.dot(embedding1, embedding2) / (
                np.linalg.norm(embedding1) * np.linalg.norm(embedding2)
            )
        except Exception:
            return 0.0

    def check(
        self, prompt: str, model: str, case_id: Optional[str] = None
    ) -> Optional[LLMResponse]:
        """Check cache for semantically similar response, scoped to case_id."""

        # Simple hash-based cache if no embeddings
        if not self.encoder:
            cache_key = self._get_cache_key(prompt, model, case_id)
            if cache_key in self.cache:
                cache_entry = self.cache[cache_key]
                return LLMResponse(
                    content=cache_entry["content"],
                    confidence=cache_entry["confidence"],
                    provider=cache_entry["provider"],
                    model=cache_entry["model"],
                    tokens_used=cache_entry["tokens_used"],
                    response_time_ms=0,
                    cached=True,
                )
            return None

        # Semantic similarity cache
        prompt_embedding = self._compute_embedding(prompt)
        if prompt_embedding is None:
            return None

        # Find most similar cached response, restricted to same case and model
        best_similarity = 0.0
        best_response = None

        for cache_key, cache_entry in self.cache.items():
            if cache_entry["model"] != model:
                continue
            # Strict case isolation: never serve a cached response across cases
            if cache_entry.get("case_id") != case_id:
                continue

            cached_embedding = self.embeddings.get(cache_key)
            if cached_embedding is None:
                continue

            similarity = self._compute_similarity(prompt_embedding, cached_embedding)
            if similarity > best_similarity and similarity >= self.similarity_threshold:
                best_similarity = similarity
                best_response = cache_entry

        if best_response:
            return LLMResponse(
                content=best_response["content"],
                confidence=best_response["confidence"],
                provider=best_response["provider"],
                model=best_response["model"],
                tokens_used=best_response["tokens_used"],
                response_time_ms=0,  # Cached response
                cached=True,
            )

        return None

    def store(
        self,
        prompt: str,
        model: str,
        response: LLMResponse,
        case_id: Optional[str] = None,
    ):
        """Store response in cache, tagged with case_id."""
        cache_key = self._get_cache_key(prompt, model, case_id)

        # Store response
        self.cache[cache_key] = {
            "content": response.content,
            "confidence": response.confidence,
            "provider": response.provider,
            "model": response.model,
            "tokens_used": response.tokens_used,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "case_id": case_id,
        }

        # Store embedding if available
        if self.encoder:
            prompt_embedding = self._compute_embedding(prompt)
            if prompt_embedding is not None:
                self.embeddings[cache_key] = prompt_embedding

        # Evict oldest entries if cache is full
        if len(self.cache) > self.max_size:
            oldest_key = min(
                self.cache.keys(), key=lambda k: self.cache[k]["timestamp"]
            )
            del self.cache[oldest_key]
            if oldest_key in self.embeddings:
                del self.embeddings[oldest_key]
