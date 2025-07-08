"""router.py

Purpose: LLM interaction management

Requirements:
--------------------------------------------------------------------------------
• Implement tiered provider strategy
• Semantic caching
• Confidence-based fallback
• Retry logic with exponential backoff

Key Components:
--------------------------------------------------------------------------------
  class LLMRouter: async route(...)
  SemanticCache: check(), store()

Technology Stack:
--------------------------------------------------------------------------------
aiohttp, Firework SDK, OpenRouter client, tenacity

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Cost-Efficiency: Use semantic caching
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

import aiohttp
import numpy as np
from sentence_transformers import SentenceTransformer
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..models import DataType
from ..observability.tracing import trace
from ..security.redaction import DataSanitizer


@dataclass
class LLMResponse:
    """Response from LLM provider"""

    content: str
    confidence: float
    provider: str
    model: str
    tokens_used: int
    response_time_ms: int
    cached: bool = False


class SemanticCache:
    """Semantic cache for LLM responses"""

    def __init__(self, similarity_threshold: float = 0.85, max_size: int = 1000):
        self.similarity_threshold = similarity_threshold
        self.max_size = max_size
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.embeddings: Dict[str, np.ndarray] = {}

        # Initialize sentence transformer for semantic similarity
        try:
            self.encoder = SentenceTransformer("BAAI/bge-m3")
        except Exception as e:
            logging.warning(f"Failed to load sentence transformer: {e}")
            self.encoder = None  # type: ignore

    def _get_cache_key(self, prompt: str, model: str) -> str:
        """Generate cache key for prompt and model"""
        content = f"{prompt}:{model}"
        return hashlib.sha256(content.encode()).hexdigest()

    def _compute_embedding(self, text: str) -> Optional[np.ndarray]:
        """Compute embedding for text"""
        if not self.encoder:
            return None
        try:
            return self.encoder.encode([text])[0]
        except Exception as e:
            logging.warning(f"Failed to compute embedding: {e}")
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

    def check(self, prompt: str, model: str) -> Optional[LLMResponse]:
        """Check cache for semantically similar response"""
        if not self.encoder:
            return None

        prompt_embedding = self._compute_embedding(prompt)
        if prompt_embedding is None:
            return None

        # Find most similar cached response
        best_similarity = 0.0
        best_response = None

        for cache_key, cache_entry in self.cache.items():
            if cache_entry["model"] != model:
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

    def store(self, prompt: str, model: str, response: LLMResponse):
        """Store response in cache"""
        cache_key = self._get_cache_key(prompt, model)

        # Store response
        self.cache[cache_key] = {
            "content": response.content,
            "confidence": response.confidence,
            "provider": response.provider,
            "model": response.model,
            "tokens_used": response.tokens_used,
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Store embedding
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


class LLMRouter:
    """Manages LLM interactions with tiered provider strategy"""

    def __init__(self, confidence_threshold: float = 0.8):
        self.logger = logging.getLogger(__name__)
        self.sanitizer = DataSanitizer()
        self.cache = SemanticCache()
        self.confidence_threshold = confidence_threshold

        # Provider configuration
        self.providers = {
            "primary": {
                "name": "fireworks",
                "api_key": None,  # Set via environment variable
                "base_url": "https://api.fireworks.ai/inference/v1",
                "models": ["accounts/fireworks/models/llama-v2-7b-chat"],
                "max_retries": 3,
                "timeout": 30,
            },
            "fallback": {
                "name": "openrouter",
                "api_key": None,  # Set via environment variable
                "base_url": "https://openrouter.ai/api/v1",
                "models": ["openai/gpt-3.5-turbo"],
                "max_retries": 2,
                "timeout": 45,
            },
            "local": {
                "name": "ollama",
                "base_url": "http://localhost:11434",
                "models": ["llama2"],
                "max_retries": 1,
                "timeout": 60,
            },
        }

        # Load API keys from environment
        self._load_api_keys()

    def _load_api_keys(self):
        """Load API keys and base URLs from environment variables"""
        import os

        # Fireworks AI
        fireworks_key = os.getenv("FIREWORKS_API_KEY")
        if fireworks_key:
            self.providers["primary"]["api_key"] = fireworks_key

        # Override base URL if provided
        fireworks_base = os.getenv("FIREWORKS_API_BASE")
        if fireworks_base:
            self.providers["primary"]["base_url"] = fireworks_base
            self.logger.info(f"Fireworks base URL set to: {fireworks_base}")

        # OpenRouter
        openrouter_key = os.getenv("OPENROUTER_API_KEY")
        if openrouter_key:
            self.providers["fallback"]["api_key"] = openrouter_key

        # Override base URL if provided
        openrouter_base = os.getenv("OPENROUTER_API_BASE")
        if openrouter_base:
            self.providers["fallback"]["base_url"] = openrouter_base
            self.logger.info(f"OpenRouter base URL set to: {openrouter_base}")

        # Ollama
        ollama_base = os.getenv("OLLAMA_API_BASE")
        if ollama_base:
            self.providers["local"]["base_url"] = ollama_base
            self.logger.info(f"Ollama base URL set to: {ollama_base}")

        # Debug: log final configuration
        self.logger.info(f"Final provider configuration:")
        for key, config in self.providers.items():
            self.logger.info(f"  {key}: {config['base_url']}")

    @trace("llm_router_route")
    async def route(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        data_type: Optional[DataType] = None,
    ) -> LLMResponse:
        """
        Route request to appropriate LLM provider with confidence-based fallback

        Args:
            prompt: Input prompt
            model: Specific model to use (optional)
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            data_type: Type of data being processed

        Returns:
            LLMResponse with generated content
        """
        # Sanitize prompt before sending to external providers
        sanitized_prompt = self.sanitizer.sanitize(prompt)

        # Check cache first
        if model:
            cached_response = self.cache.check(sanitized_prompt, model)
            if cached_response:
                self.logger.info("Using cached response")
                return cached_response

        # Try providers in order: primary -> fallback -> local
        providers_to_try = ["primary", "fallback", "local"]

        for provider_key in providers_to_try:
            provider_config = self.providers[provider_key]

            # Skip if API key is required but not available
            if (
                provider_config["name"] in ["fireworks", "openrouter"]
                and not provider_config["api_key"]
            ):
                self.logger.warning(f"Skipping {provider_key} - no API key")
                continue

            try:
                response = await self._call_provider(
                    provider_key,
                    sanitized_prompt,
                    model or provider_config["models"][0],
                    max_tokens,
                    temperature,
                )

                # Check confidence before returning or continuing to next provider
                if response and response.confidence >= self.confidence_threshold:
                    # Store high-confidence response in cache
                    self.cache.store(
                        sanitized_prompt,
                        model or provider_config["models"][0],
                        response,
                    )
                    self.logger.info(
                        f"High confidence response from {provider_key} (confidence: {response.confidence})"
                    )
                    return response
                else:
                    # Log low confidence and continue to next provider
                    if response:
                        self.logger.warning(
                            f"Low confidence response from {provider_key} "
                            f"(confidence: {response.confidence} < "
                            f"{self.confidence_threshold}). Trying next provider."
                        )
                    continue

            except Exception as e:
                self.logger.warning(f"Provider {provider_key} failed: {e}")
                continue

        # If all providers fail, return error response
        raise Exception("All LLM providers failed or returned low confidence responses")

    @trace("llm_router_call_provider")
    async def _call_provider(
        self,
        provider_key: str,
        prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        """Call specific LLM provider with retry logic"""
        provider_config = self.providers[provider_key]
        max_retries = provider_config.get("max_retries", 1)

        # Create retry decorator with provider-specific max_retries
        retry_decorator = retry(
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(
                (aiohttp.ClientError, TimeoutError, ConnectionError)
            ),
        )

        @retry_decorator
        async def _make_call():
            if provider_config["name"] == "fireworks":
                return await self._call_fireworks(
                    prompt, model, max_tokens, temperature
                )
            elif provider_config["name"] == "openrouter":
                return await self._call_openrouter(
                    prompt, model, max_tokens, temperature
                )
            elif provider_config["name"] == "ollama":
                return await self._call_ollama(prompt, model, max_tokens, temperature)
            else:
                raise ValueError(f"Unknown provider: {provider_config['name']}")

        return await _make_call()

    async def _call_fireworks(
        self, prompt: str, model: str, max_tokens: int, temperature: float
    ) -> LLMResponse:
        """Call Fireworks AI API"""
        provider_config = self.providers["primary"]
        start_time = time.time()

        headers = {
            "Authorization": f'Bearer {provider_config["api_key"]}',
            "Content-Type": "application/json",
        }

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{provider_config['base_url']}/chat/completions",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=provider_config["timeout"]),
            ) as response:
                if response.status != 200:
                    raise Exception(f"Fireworks API error: {response.status}")

                data = await response.json()
                content = data["choices"][0]["message"]["content"]

                # Validate content is not None or empty
                if content is None or content == "":
                    raise Exception("Fireworks API returned empty content")

                tokens_used = data["usage"]["total_tokens"]
                response_time = int((time.time() - start_time) * 1000)

                return LLMResponse(
                    content=content,
                    confidence=0.9,  # High confidence for primary provider
                    provider="fireworks",
                    model=model,
                    tokens_used=tokens_used,
                    response_time_ms=response_time,
                )

    async def _call_openrouter(
        self, prompt: str, model: str, max_tokens: int, temperature: float
    ) -> LLMResponse:
        """Call OpenRouter API"""
        provider_config = self.providers["fallback"]
        start_time = time.time()

        headers = {
            "Authorization": f'Bearer {provider_config["api_key"]}',
            "Content-Type": "application/json",
        }

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{provider_config['base_url']}/chat/completions",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=provider_config["timeout"]),
            ) as response:
                if response.status != 200:
                    raise Exception(f"OpenRouter API error: {response.status}")

                data = await response.json()
                content = data["choices"][0]["message"]["content"]

                # Validate content is not None or empty
                if content is None or content == "":
                    raise Exception("OpenRouter API returned empty content")

                tokens_used = data["usage"]["total_tokens"]
                response_time = int((time.time() - start_time) * 1000)

                return LLMResponse(
                    content=content,
                    confidence=0.8,  # Good confidence for fallback
                    provider="openrouter",
                    model=model,
                    tokens_used=tokens_used,
                    response_time_ms=response_time,
                )

    async def _call_ollama(
        self, prompt: str, model: str, max_tokens: int, temperature: float
    ) -> LLMResponse:
        """Call local Ollama API"""
        provider_config = self.providers["local"]
        start_time = time.time()

        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": max_tokens, "temperature": temperature},
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{provider_config['base_url']}/api/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=provider_config["timeout"]),
            ) as response:
                if response.status != 200:
                    raise Exception(f"Ollama API error: {response.status}")

                data = await response.json()
                content = data["response"]

                # Validate content is not None or empty
                if content is None or content == "":
                    raise Exception("Ollama API returned empty content")

                tokens_used = data.get("eval_count", 0)
                response_time = int((time.time() - start_time) * 1000)

                return LLMResponse(
                    content=content,
                    confidence=0.6,  # Lower confidence for local model
                    provider="ollama",
                    model=model,
                    tokens_used=tokens_used,
                    response_time_ms=response_time,
                )
