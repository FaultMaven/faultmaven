"""
Base provider interface for LLM providers.

This module defines the abstract base class that all LLM providers must implement,
ensuring consistent behavior and configuration across all provider implementations.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    """Tool/function call from LLM"""

    id: str
    type: str  # "function"
    function: Dict[str, Any]  # {"name": "...", "arguments": "..."}


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
    tool_calls: Optional[List[ToolCall]] = None  # Function calling support


@dataclass
class ProviderConfig:
    """Configuration for an LLM provider"""
    
    name: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    models: List[str] = None
    max_retries: int = 3
    timeout: int = 30
    default_model: Optional[str] = None
    confidence_score: float = 0.8
    
    def __post_init__(self):
        if self.models is None:
            self.models = []
        if self.default_model is None and self.models:
            self.default_model = self.models[0]


class BaseLLMProvider(ABC):
    """Abstract base class for all LLM providers"""
    
    def __init__(self, config: ProviderConfig):
        self.config = config
        self.start_time = None
    
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the unique name of this provider"""
        pass
    
    @abstractmethod
    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        **kwargs
    ) -> LLMResponse:
        """
        Generate a response using this provider
        
        Args:
            prompt: Input prompt
            model: Specific model to use (optional)
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            **kwargs: Additional provider-specific parameters
            
        Returns:
            LLMResponse with generated content
        """
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if the provider is properly configured and available"""
        pass
    
    @abstractmethod
    def get_supported_models(self) -> List[str]:
        """Get list of models supported by this provider"""
        pass
    
    def _start_timing(self):
        """Start timing for response measurement"""
        self.start_time = time.time()
    
    def _get_response_time_ms(self) -> int:
        """Get response time in milliseconds"""
        if self.start_time is None:
            return 0
        return int((time.time() - self.start_time) * 1000)
    
    def _validate_response_content(self, content: str) -> str:
        """Validate and clean response content"""
        if content is None:
            raise ValueError(f"{self.provider_name} returned None content")
        
        content = content.strip()
        if not content:
            raise ValueError(f"{self.provider_name} returned empty content")
        
        return content
    
    def get_effective_model(self, requested_model: Optional[str] = None) -> str:
        """Get the model to use, with fallback logic"""
        if requested_model and requested_model in self.config.models:
            return requested_model
        
        if self.config.default_model:
            return self.config.default_model
        
        if self.config.models:
            return self.config.models[0]
        
        raise ValueError(f"No valid model available for provider {self.provider_name}")