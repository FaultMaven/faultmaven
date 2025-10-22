"""
Local LLM provider implementation.

This module implements the local LLM provider for self-hosted models
including Phi-3, Ollama, and other local inference servers.
"""

import asyncio
import aiohttp
import logging
from typing import List, Optional

from .base import BaseLLMProvider, LLMResponse, ProviderConfig


class LocalProvider(BaseLLMProvider):
    """Local LLM provider implementation"""
    
    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    def provider_name(self) -> str:
        return "local"

    def is_available(self) -> bool:
        """Check if local provider is properly configured"""
        return bool(
            self.config.base_url and
            self.config.models
        )

    def get_supported_models(self) -> List[str]:
        """Get list of supported models"""
        return self.config.models.copy()

    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        **kwargs
    ) -> LLMResponse:
        """Generate response using local LLM server"""

        self._start_timing()

        # Get effective model
        effective_model = self.get_effective_model(model)

        # Intelligently detect API format for optimal compatibility
        # Priority order: Ollama -> OpenAI-compatible -> Raw llama.cpp

        if "ollama" in self.config.base_url.lower() or "ollama" in effective_model.lower():
            # Ollama-specific API
            return await self._call_ollama_api(prompt, effective_model, max_tokens, temperature, **kwargs)

        # First try OpenAI-compatible API (most common for modern local LLM servers)
        try:
            return await self._call_openai_compatible_api(prompt, effective_model, max_tokens, temperature, **kwargs)
        except Exception as openai_error:
            # If OpenAI-compatible fails, try raw llama.cpp completion endpoint as fallback
            if "404" in str(openai_error) or "not found" in str(openai_error).lower():
                try:
                    return await self._call_llamacpp_api(prompt, effective_model, max_tokens, temperature, **kwargs)
                except Exception as llamacpp_error:
                    # If both fail, raise the more informative error
                    raise Exception(f"Local LLM server failed with both API formats. OpenAI-compatible: {openai_error}. Raw llama.cpp: {llamacpp_error}")
            else:
                # For non-404 errors, re-raise the OpenAI error
                raise openai_error

    async def _call_ollama_api(
        self,
        prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
        **kwargs
    ) -> LLMResponse:
        """Call Ollama-style API"""

        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature
            }
        }

        # Add any additional options
        if kwargs:
            payload["options"].update(kwargs)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.config.base_url}/api/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout),
            ) as response:

                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(
                        f"Ollama API error {response.status}: {error_text}"
                    )

                data = await response.json()

                # Extract response content
                content = data.get("response")
                if not content:
                    raise Exception("Ollama API returned no response content")

                content = self._validate_response_content(content)

                # Extract token usage (Ollama specific)
                tokens_used = data.get("eval_count", 0)

                response_time = self._get_response_time_ms()

                return LLMResponse(
                    content=content,
                    confidence=self.config.confidence_score,
                    provider=self.provider_name,
                    model=model,
                    tokens_used=tokens_used,
                    response_time_ms=response_time,
                )

    async def _call_openai_compatible_api(
        self,
        prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
        **kwargs
    ) -> LLMResponse:
        """Call OpenAI-compatible API (for llama.cpp with OpenAI API, Phi-3 ONNX and similar)"""

        self.logger.debug(f"Starting OpenAI-compatible API call to {self.config.base_url}")
        self.logger.debug(f"Model: {model}, Max tokens: {max_tokens}, Temperature: {temperature}")

        headers = {
            "Content-Type": "application/json",
        }

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Add any additional kwargs
        payload.update(kwargs)

        self.logger.debug(f"Request payload: {payload}")

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    f"{self.config.base_url}/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.config.timeout),
                ) as response:

                    self.logger.debug(f"Response status: {response.status}")

                    if response.status != 200:
                        error_text = await response.text()
                        error_msg = f"Local OpenAI-compatible API error {response.status}: {error_text}"
                        self.logger.error(f"HTTP Error: {error_msg}")
                        raise Exception(error_msg)

                    data = await response.json()
                    self.logger.debug(f"Response data: {data}")

                    # Extract response content
                    if not data.get("choices") or len(data["choices"]) == 0:
                        error_msg = "Local OpenAI-compatible API returned no choices"
                        self.logger.error(f"No choices: {error_msg}")
                        raise Exception(error_msg)

                    content = data["choices"][0]["message"]["content"]
                    self.logger.debug(f"Raw content: {repr(content)}")

                    try:
                        content = self._validate_response_content(content)
                        self.logger.debug(f"Validated content: {repr(content)}")
                    except Exception as e:
                        self.logger.error(f"Content validation failed: {e}")
                        raise

                    # Extract token usage
                    usage = data.get("usage", {})
                    tokens_used = usage.get("total_tokens", 0)

                    response_time = self._get_response_time_ms()

                    self.logger.info(f"Successful response with {tokens_used} tokens, {response_time}ms")

                    return LLMResponse(
                        content=content,
                        confidence=self.config.confidence_score,
                        provider=self.provider_name,
                        model=model,
                        tokens_used=tokens_used,
                        response_time_ms=response_time,
                    )

            except asyncio.TimeoutError as e:
                response_time = self._get_response_time_ms()
                self.logger.warning(f"Timeout after {response_time}ms (limit: {self.config.timeout * 1000}ms)")
                self.logger.warning(f"Model: {model}, Max tokens: {max_tokens}, Temperature: {temperature}")
                self.logger.debug(f"Timeout error: {e}")
                raise Exception(f"Local LLM request timed out after {self.config.timeout} seconds")

            except Exception as e:
                response_time = self._get_response_time_ms()
                self.logger.error(f"Request failed after {response_time}ms")
                self.logger.error(f"Error type: {type(e).__name__}")
                self.logger.error(f"Error details: {e}")
                raise

    async def _call_llamacpp_api(
        self,
        prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
        **kwargs
    ) -> LLMResponse:
        """Call raw llama.cpp server API (completions endpoint)"""

        # llama.cpp server uses completions endpoint, not chat/completions
        payload = {
            "prompt": prompt,
            "n_predict": max_tokens,
            "temperature": temperature,
            "stop": ["\\n\\n"],  # Basic stop tokens
            "stream": False
        }

        # Add any additional options
        if kwargs:
            payload.update(kwargs)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.config.base_url}/completion",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout),
            ) as response:

                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(
                        f"Raw llama.cpp server API error {response.status}: {error_text}"
                    )

                data = await response.json()

                # Extract response content
                content = data.get("content")
                if not content:
                    raise Exception("Raw llama.cpp server API returned no content")

                content = self._validate_response_content(content)

                # Extract token usage (llama.cpp specific)
                tokens_used = data.get("tokens_predicted", 0)

                response_time = self._get_response_time_ms()

                return LLMResponse(
                    content=content,
                    confidence=self.config.confidence_score,
                    provider=self.provider_name,
                    model=model,
                    tokens_used=tokens_used,
                    response_time_ms=response_time,
                )