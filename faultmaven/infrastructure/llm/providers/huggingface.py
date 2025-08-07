"""
Hugging Face provider implementation.

This module implements the Hugging Face Inference API provider for
accessing open-source models and specialized models.
"""

import json
import time
from typing import List, Optional

import aiohttp

from .base import BaseLLMProvider, LLMResponse, ProviderConfig


class HuggingFaceProvider(BaseLLMProvider):
    """Hugging Face Inference API provider implementation"""
    
    @property
    def provider_name(self) -> str:
        return "huggingface"
    
    def is_available(self) -> bool:
        """Check if Hugging Face provider is properly configured"""
        return bool(
            self.config.api_key and
            self.config.base_url and
            self.config.models
        )
    
    def get_supported_models(self) -> List[str]:
        """Get list of supported Hugging Face models"""
        return self.config.models.copy()
    
    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        **kwargs
    ) -> LLMResponse:
        """
        Generate text using Hugging Face Inference API
        
        Args:
            prompt: Input prompt for text generation
            model: Specific Hugging Face model to use
            max_tokens: Maximum tokens to generate (mapped to max_new_tokens)
            temperature: Sampling temperature (0.0-2.0)
            **kwargs: Additional parameters
            
        Returns:
            LLMResponse with generated text
        """
        start_time = time.time()
        
        # Use specified model or default
        selected_model = model or self.config.default_model
        if not selected_model:
            selected_model = "microsoft/DialoGPT-medium"
        
        # Prepare headers for Hugging Face API
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json"
        }
        
        # Prepare request body for Hugging Face API format
        request_body = {
            "inputs": prompt,
            "parameters": {
                "max_new_tokens": max_tokens,
                "temperature": temperature,
                "return_full_text": False,
                "do_sample": True
            }
        }
        
        # Add optional parameters
        if "top_p" in kwargs:
            request_body["parameters"]["top_p"] = kwargs["top_p"]
        if "top_k" in kwargs:
            request_body["parameters"]["top_k"] = kwargs["top_k"]
        if "repetition_penalty" in kwargs:
            request_body["parameters"]["repetition_penalty"] = kwargs["repetition_penalty"]
        if "stop_sequences" in kwargs:
            request_body["parameters"]["stop"] = kwargs["stop_sequences"]
        
        # Add wait_for_model parameter to handle model loading
        request_body["options"] = {"wait_for_model": True}
        
        # Make API request to Hugging Face
        url = f"{self.config.base_url.rstrip('/')}/{selected_model}"
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                json=request_body,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout)
            ) as response:
                
                if response.status == 503:
                    # Model is loading, wait and retry once
                    await self._handle_model_loading(session, url, headers, request_body)
                    return await self._retry_request(session, url, headers, request_body, start_time, selected_model)
                
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(
                        f"Hugging Face API request failed: {response.status} - {error_text}"
                    )
                
                response_data = await response.json()
        
        # Extract content from Hugging Face response format
        content = ""
        if isinstance(response_data, list) and len(response_data) > 0:
            # Standard text generation response
            first_result = response_data[0]
            if "generated_text" in first_result:
                content = first_result["generated_text"]
            elif "text" in first_result:
                content = first_result["text"]
        elif isinstance(response_data, dict):
            # Some models return different formats
            if "generated_text" in response_data:
                content = response_data["generated_text"]
            elif "text" in response_data:
                content = response_data["text"]
        
        # Calculate metrics
        response_time_ms = int((time.time() - start_time) * 1000)
        
        # Estimate token usage (Hugging Face doesn't always provide this)
        tokens_used = self._estimate_tokens(content)
        
        # Calculate confidence based on model and response quality
        confidence = self._calculate_confidence(selected_model, content, response_data)
        
        return LLMResponse(
            content=content,
            confidence=confidence,
            provider=self.provider_name,
            model=selected_model,
            tokens_used=tokens_used,
            response_time_ms=response_time_ms,
            cached=False
        )
    
    async def _handle_model_loading(self, session: aiohttp.ClientSession, url: str, headers: dict, request_body: dict):
        """Handle model loading delay"""
        # Wait a bit for model to load
        import asyncio
        await asyncio.sleep(10)
    
    async def _retry_request(self, session: aiohttp.ClientSession, url: str, headers: dict, request_body: dict, start_time: float, selected_model: str) -> LLMResponse:
        """Retry request after model loading"""
        async with session.post(
            url,
            headers=headers,
            json=request_body,
            timeout=aiohttp.ClientTimeout(total=self.config.timeout)
        ) as response:
            
            if response.status != 200:
                error_text = await response.text()
                raise Exception(
                    f"Hugging Face API request failed on retry: {response.status} - {error_text}"
                )
            
            response_data = await response.json()
        
        # Extract content from retry response
        content = ""
        if isinstance(response_data, list) and len(response_data) > 0:
            first_result = response_data[0]
            if "generated_text" in first_result:
                content = first_result["generated_text"]
            elif "text" in first_result:
                content = first_result["text"]
        
        response_time_ms = int((time.time() - start_time) * 1000)
        tokens_used = self._estimate_tokens(content)
        confidence = self._calculate_confidence(selected_model, content, response_data)
        
        return LLMResponse(
            content=content,
            confidence=confidence,
            provider=self.provider_name,
            model=selected_model,
            tokens_used=tokens_used,
            response_time_ms=response_time_ms,
            cached=False
        )
    
    def _estimate_tokens(self, content: str) -> int:
        """
        Estimate token count for content (rough approximation)
        
        Args:
            content: Generated content
            
        Returns:
            Estimated token count
        """
        # Rough approximation: ~4 characters per token for English text
        return max(1, len(content) // 4)
    
    def _calculate_confidence(self, model: str, content: str, response_data: dict) -> float:
        """
        Calculate confidence score for Hugging Face response
        
        Args:
            model: Model used for generation
            content: Generated content
            response_data: Full API response
            
        Returns:
            Confidence score (0.0-1.0)
        """
        base_confidence = self.config.confidence_score
        
        # Hugging Face models have varying confidence characteristics
        model_confidence_map = {
            # Large language models
            "microsoft/dialoGPT": 0.75,
            "gpt2": 0.70,
            "distilgpt2": 0.65,
            "facebook/blenderbot": 0.78,
            "microsoft/DialoGPT-large": 0.80,
            "microsoft/DialoGPT-medium": 0.75,
            "microsoft/DialoGPT-small": 0.70,
            # Instruction-following models
            "tiiuae/falcon": 0.85,
            "bigscience/bloom": 0.82,
            "EleutherAI/gpt-j": 0.80,
            # Code models
            "Salesforce/codegen": 0.78,
            "microsoft/codebert": 0.75,
        }
        
        # Find matching model confidence
        model_confidence = base_confidence
        for model_name, confidence in model_confidence_map.items():
            if model_name.lower() in model.lower():
                model_confidence = confidence
                break
        
        # Adjust based on content quality
        content_length = len(content.strip())
        
        if content_length == 0:
            return 0.0
        elif content_length < 30:
            # Very short responses might be less reliable
            model_confidence *= 0.7
        elif content_length > 300:
            # Longer responses from smaller models might be less coherent
            model_confidence *= 0.95
        
        # Check for common generation issues
        problematic_patterns = [
            "unk>", "<pad>", "<eos>", "[UNK]", "[PAD]", "[EOS]",
            "sorry, i", "i cannot", "i can't", "not able to"
        ]
        
        content_lower = content.lower()
        for pattern in problematic_patterns:
            if pattern in content_lower:
                model_confidence *= 0.6
                break
        
        # Check for repetitive content (common issue with smaller models)
        words = content.split()
        if len(words) > 10:
            unique_words = set(words)
            repetition_ratio = len(unique_words) / len(words)
            if repetition_ratio < 0.7:  # High repetition
                model_confidence *= 0.8
        
        # Check if response seems incomplete or cut off
        if content.endswith(("...", ".", ",")):
            # Might be a natural ending
            pass
        elif len(content) > 100 and not content.endswith((".", "!", "?", "\n")):
            # Long response without proper ending - might be truncated
            model_confidence *= 0.9
        
        # Ensure confidence is within valid range
        return min(1.0, max(0.0, model_confidence))