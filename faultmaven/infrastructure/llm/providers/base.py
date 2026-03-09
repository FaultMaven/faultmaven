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
)


@dataclass
class ToolCall:
    """Tool/function call from LLM"""

    id: str
    type: str  # "function"
    function: Dict[str, Any]  # {"name": "...", "arguments": "..."}


@dataclass
class NormalizedResponse:
    """
    Normalized LLM response with validity checking.

    This replaces the old confidence-based system with explicit validity checks.
    Provider layer determines if response is actionable; orchestration layer
    assesses quality on normalized output.

    Design principles:
    - Binary validity check: is_valid property
    - Separates network/API failures from content refusals
    - Distinguishes hard refusals from helpful hedged responses
    - Provider-agnostic structure
    """

    provider: str
    model: str
    content: str = ""
    tool_calls: List[ToolCall] = None
    usage: Dict[str, int] = None
    response_time_ms: int = 0
    cached: bool = False
    raw_response: Any = None

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = []
        if self.usage is None:
            self.usage = {}

    @property
    def is_valid(self) -> bool:
        """
        Determines if the response is actionable.

        A response is VALID if:
        - Has non-empty content OR has tool_calls
        - Does not contain a hard refusal

        A response is INVALID if:
        - Both content and tool_calls are empty
        - Contains hard refusal ("I cannot help", "I'm unable to assist")

        IMPORTANT: Hedged responses are VALID:
        - "I cannot verify X without Y, but here's what I can tell you..."
        - "I'm not able to see Z, but based on the evidence..."

        Returns:
            bool: True if response is actionable, False otherwise
        """
        # Check for empty response
        has_content = bool(self.content.strip())
        has_tool_calls = bool(self.tool_calls)

        if not has_content and not has_tool_calls:
            return False

        # Check for hard refusals (complete inability to help)
        if has_content:
            content_lower = self.content.lower()

            # Hard refusal patterns - these invalidate the entire response
            hard_refusal_patterns = [
                "i cannot help",
                "i'm unable to assist",
                "i can't assist",
                "i'm not able to help",
                "i don't have access to",
                "i'm sorry, but i cannot",
                "i apologize, but i cannot",
            ]

            # Check if response starts with hard refusal (first 200 chars)
            response_start = content_lower[:200]
            for pattern in hard_refusal_patterns:
                if pattern in response_start:
                    # Check if there's substantial content after the hedge
                    # If response is short (<150 chars), it's a pure refusal
                    if len(self.content.strip()) < 150:
                        return False

                    # If response is longer, check if it's actually providing value
                    # Look for value indicators after the hedge
                    value_indicators = [
                        "however",
                        "but",
                        "although",
                        "based on",
                        "here's what",
                        "i can tell you",
                        "i notice",
                        "the evidence shows",
                    ]

                    has_value_after_hedge = any(
                        indicator in content_lower for indicator in value_indicators
                    )

                    if not has_value_after_hedge:
                        return False

        # Response is valid if we got here
        return True

    @property
    def has_structured_output(self) -> bool:
        """Check if response contains structured output (tool calls)."""
        return bool(self.tool_calls)

    @property
    def is_text_only(self) -> bool:
        """Check if response is text-only (no tool calls)."""
        return bool(self.content.strip()) and not self.tool_calls


@dataclass
class LLMResponse:
    """
    Legacy response structure - DEPRECATED

    This will be replaced by NormalizedResponse in upcoming refactor.
    Kept for backward compatibility during migration.
    """

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

    def supports_tool_calling(self, model: Optional[str] = None) -> bool:
        """Whether this provider/model supports function calling (tools API).

        Default returns True since most providers support OpenAI-compatible
        tool calling. Providers should override for models that don't support it.

        Args:
            model: Model name to check (uses default if None)

        Returns:
            True if tool calling is supported
        """
        return True

    def get_structured_output_capability(
        self, model: Optional[str] = None
    ) -> StructuredOutputCapability:
        """
        Get the structured output capability for this provider/model.

        **IMPORTANT: All provider subclasses MUST override this method.**

        This method determines what level of structured output support is available
        for the given model. Each provider implements provider-specific logic to
        detect capabilities based on model names and features.

        **Provider Override Pattern:**
        Subclasses MUST override this method to provide accurate capability detection:

        ```python
        def get_structured_output_capability(
            self, model: Optional[str] = None
        ) -> StructuredOutputCapability:
            effective_model = self.get_effective_model(model)

            # Provider-specific logic
            if "model-with-strict-support" in effective_model:
                return StructuredOutputCapability.STRICT
            elif "model-with-function-calling" in effective_model:
                return StructuredOutputCapability.FUNCTION_CALLING
            else:
                return StructuredOutputCapability.BEST_EFFORT
        ```

        **Default Behavior:**
        If not overridden, returns BEST_EFFORT as a conservative fallback and logs
        a warning that the provider should implement this method.

        Args:
            model: Model name to check (uses default if None)

        Returns:
            StructuredOutputCapability: One of STRICT, FUNCTION_CALLING, BEST_EFFORT, or NONE

        Design Reference:
            Template Method pattern - base provides default, subclasses specialize
        """
        # Conservative fallback for providers that haven't implemented this yet
        self.logger.warning(
            f"{self.provider_name} provider has not overridden get_structured_output_capability(). "
            f"Returning BEST_EFFORT as conservative default. Provider should implement this method."
        )
        return StructuredOutputCapability.BEST_EFFORT

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
