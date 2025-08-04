"""
Local LLM provider implementation.

This module implements the local LLM provider for self-hosted models
including Phi-3, Ollama, and other local inference servers.
"""

import aiohttp
from typing import List, Optional

from .base import BaseLLMProvider, LLMResponse, ProviderConfig


class LocalProvider(BaseLLMProvider):
    """Local LLM provider implementation"""
    
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
        
        # Try different API formats based on the base URL or model
        if "ollama" in self.config.base_url.lower() or "ollama" in effective_model.lower():
            return await self._call_ollama_api(prompt, effective_model, max_tokens, temperature, **kwargs)
        else:
            return await self._call_openai_compatible_api(prompt, effective_model, max_tokens, temperature, **kwargs)
    
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
        """Call OpenAI-compatible API (for Phi-3 ONNX and similar)"""
        
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
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.config.base_url}/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout),
            ) as response:
                
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(
                        f"Local API error {response.status}: {error_text}"
                    )
                
                data = await response.json()
                
                # Extract response content
                if not data.get("choices") or len(data["choices"]) == 0:
                    raise Exception("Local API returned no choices")
                
                content = data["choices"][0]["message"]["content"]
                content = self._validate_response_content(content)
                
                # Extract token usage
                usage = data.get("usage", {})
                tokens_used = usage.get("total_tokens", 0)
                
                response_time = self._get_response_time_ms()
                
                return LLMResponse(
                    content=content,
                    confidence=self.config.confidence_score,
                    provider=self.provider_name,
                    model=model,
                    tokens_used=tokens_used,
                    response_time_ms=response_time,
                )