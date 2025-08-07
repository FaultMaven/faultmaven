"""
Google Gemini provider implementation.

This module implements the Google Gemini LLM provider with multi-modal
capabilities for text and image processing.
"""

import json
import time
from typing import List, Optional

import aiohttp

from .base import BaseLLMProvider, LLMResponse, ProviderConfig


class GeminiProvider(BaseLLMProvider):
    """Google Gemini LLM provider implementation"""
    
    @property
    def provider_name(self) -> str:
        return "gemini"
    
    def is_available(self) -> bool:
        """Check if Gemini provider is properly configured"""
        return bool(
            self.config.api_key and
            self.config.base_url and
            self.config.models
        )
    
    def get_supported_models(self) -> List[str]:
        """Get list of supported Gemini models"""
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
        Generate text using Google Gemini API
        
        Args:
            prompt: Input prompt for text generation
            model: Specific Gemini model to use
            max_tokens: Maximum tokens to generate (mapped to maxOutputTokens)
            temperature: Sampling temperature (0.0-2.0)
            **kwargs: Additional parameters
            
        Returns:
            LLMResponse with generated text
        """
        start_time = time.time()
        
        # Use specified model or default
        selected_model = model or self.config.default_model
        if not selected_model:
            selected_model = "gemini-1.5-pro"
        
        # Prepare generation config for Gemini API
        generation_config = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        
        # Add optional parameters
        if "top_p" in kwargs:
            generation_config["topP"] = kwargs["top_p"]
        if "top_k" in kwargs:
            generation_config["topK"] = kwargs["top_k"]
        if "stop_sequences" in kwargs:
            generation_config["stopSequences"] = kwargs["stop_sequences"]
        
        # Prepare request body for Gemini API format
        request_body = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": prompt
                        }
                    ]
                }
            ],
            "generationConfig": generation_config
        }
        
        # Add safety settings if provided
        if "safety_settings" in kwargs:
            request_body["safetySettings"] = kwargs["safety_settings"]
        else:
            # Default safety settings for troubleshooting use case
            request_body["safetySettings"] = [
                {
                    "category": "HARM_CATEGORY_HARASSMENT",
                    "threshold": "BLOCK_MEDIUM_AND_ABOVE"
                },
                {
                    "category": "HARM_CATEGORY_HATE_SPEECH", 
                    "threshold": "BLOCK_MEDIUM_AND_ABOVE"
                },
                {
                    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "threshold": "BLOCK_MEDIUM_AND_ABOVE"
                },
                {
                    "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "threshold": "BLOCK_MEDIUM_AND_ABOVE"
                }
            ]
        
        # Make API request to Gemini
        url = f"{self.config.base_url.rstrip('/')}/models/{selected_model}:generateContent"
        
        # Add API key as query parameter (Gemini API format)
        params = {"key": self.config.api_key}
        
        headers = {
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                params=params,
                headers=headers,
                json=request_body,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout)
            ) as response:
                
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(
                        f"Gemini API request failed: {response.status} - {error_text}"
                    )
                
                response_data = await response.json()
        
        # Extract content from Gemini response format
        content = ""
        tokens_used = 0
        
        if "candidates" in response_data and response_data["candidates"]:
            candidate = response_data["candidates"][0]
            
            if "content" in candidate and "parts" in candidate["content"]:
                for part in candidate["content"]["parts"]:
                    if "text" in part:
                        content += part["text"]
            
            # Extract token usage if available
            if "usageMetadata" in response_data:
                tokens_used = response_data["usageMetadata"].get("candidatesTokenCount", 0)
        
        # Handle potential safety blocks or other issues
        if not content and "candidates" in response_data:
            candidate = response_data["candidates"][0]
            if "finishReason" in candidate:
                finish_reason = candidate["finishReason"]
                if finish_reason in ["SAFETY", "BLOCKED_REASON_UNSPECIFIED"]:
                    content = "[Content blocked by safety filters]"
                elif finish_reason == "MAX_TOKENS":
                    content = "[Response truncated due to token limit]"
        
        # Calculate metrics
        response_time_ms = int((time.time() - start_time) * 1000)
        
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
    
    def _calculate_confidence(self, model: str, content: str, response_data: dict) -> float:
        """
        Calculate confidence score for Gemini response
        
        Args:
            model: Model used for generation
            content: Generated content
            response_data: Full API response
            
        Returns:
            Confidence score (0.0-1.0)
        """
        base_confidence = self.config.confidence_score
        
        # Gemini models have different confidence characteristics
        model_confidence_map = {
            "gemini-1.5-pro": 0.92,
            "gemini-1.5-flash": 0.87,
            "gemini-1.0-pro": 0.85,
            "gemini-pro": 0.85,
            "gemini-pro-vision": 0.90  # Higher for multi-modal
        }
        
        # Find matching model confidence
        model_confidence = base_confidence
        for model_name, confidence in model_confidence_map.items():
            if model_name in model.lower():
                model_confidence = confidence
                break
        
        # Check if content was blocked by safety filters
        if "[Content blocked by safety filters]" in content:
            return 0.1  # Very low confidence for blocked content
        
        if "[Response truncated due to token limit]" in content:
            return 0.5  # Medium confidence for truncated responses
        
        # Adjust based on content quality
        content_length = len(content.strip())
        
        if content_length == 0:
            return 0.0
        elif content_length < 50:
            # Very short responses might be less reliable
            model_confidence *= 0.8
        elif content_length > 500:
            # Longer, more detailed responses are often higher quality
            model_confidence *= 1.03
        
        # Check for finish reason in response
        if "candidates" in response_data and response_data["candidates"]:
            candidate = response_data["candidates"][0]
            finish_reason = candidate.get("finishReason", "")
            
            if finish_reason == "STOP":
                # Natural completion - high confidence
                model_confidence *= 1.05
            elif finish_reason == "MAX_TOKENS":
                # Truncated - moderate confidence
                model_confidence *= 0.9
            elif finish_reason in ["SAFETY", "OTHER"]:
                # Problematic completion - low confidence
                model_confidence *= 0.4
        
        # Check for refusal or inability to answer
        refusal_indicators = [
            "i cannot", "i can't", "i'm not able", "i don't have",
            "i'm sorry", "i apologize", "i cannot provide"
        ]
        
        content_lower = content.lower()
        for indicator in refusal_indicators:
            if indicator in content_lower:
                model_confidence *= 0.7
                break
        
        # Ensure confidence is within valid range
        return min(1.0, max(0.0, model_confidence))