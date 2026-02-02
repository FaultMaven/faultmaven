"""
Base provider interface for LLM providers.

This module defines the abstract base class that all LLM providers must implement,
ensuring consistent behavior and configuration across all provider implementations.
"""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Import structured output capability system
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputStrategy,
    create_strategy_for_capability,
    get_capability_for_provider_and_model,
)


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
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

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
        **kwargs,
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

    def get_structured_output_capability(
        self, model: Optional[str] = None
    ) -> StructuredOutputCapability:
        """
        Get the structured output capability for this provider/model.

        This method determines what level of structured output support is available
        for the given model. Providers can override this to provide custom logic,
        or rely on the centralized capability registry.

        Args:
            model: Model name to check (uses default if None)

        Returns:
            StructuredOutputCapability level
        """
        effective_model = self.get_effective_model(model)
        capability = get_capability_for_provider_and_model(
            self.provider_name, effective_model
        )

        # Log capability for visibility
        self.logger.debug(
            f"Structured output capability for {self.provider_name}/{effective_model}: "
            f"{capability.value}"
        )

        return capability

    def get_structured_output_strategy(
        self, schema: Dict[str, Any], model: Optional[str] = None
    ) -> StructuredOutputStrategy:
        """
        Get the appropriate structured output strategy for this provider/model.

        This method determines the best strategy for requesting structured output
        based on the model's capabilities.

        Args:
            schema: Pydantic JSON schema
            model: Model name (uses default if None)

        Returns:
            StructuredOutputStrategy with configuration
        """
        capability = self.get_structured_output_capability(model)
        strategy = create_strategy_for_capability(capability, schema)

        # Log strategy for debugging
        self.logger.info(
            f"Using structured output strategy for {self.provider_name}: "
            f"mode={strategy.mode.value}, include_schema_in_prompt={strategy.include_schema_in_prompt}"
        )

        return strategy
