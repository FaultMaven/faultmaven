"""
Centralized Provider Registry for LLM providers.

This module provides a central registry for managing LLM providers, their configurations,
and fallback strategies. It resolves the scattered configuration problem by providing
a single source of truth for provider management.
"""

import logging
import os
from typing import Dict, List, Optional, Type, Union

try:
    from dotenv import load_dotenv
    load_dotenv()  # Load environment variables when module is imported
except ImportError:
    pass  # dotenv not available, continue without it

from .base import BaseLLMProvider, ProviderConfig, LLMResponse
from .fireworks_provider import FireworksProvider
from .openai_provider import OpenAIProvider
from .local_provider import LocalProvider


# Data-driven provider schema - single source of truth
PROVIDER_SCHEMA = {
    "fireworks": {
        "api_key_var": "FIREWORKS_API_KEY",
        "model_var": "FIREWORKS_MODEL", 
        "base_url_var": "FIREWORKS_API_BASE",
        "default_base_url": "https://api.fireworks.ai/inference/v1",
        "default_model": "accounts/fireworks/models/llama-v3p1-8b-instruct",
        "provider_class": FireworksProvider,
        "max_retries": 3,
        "timeout": 30,
        "confidence_score": 0.9
    },
    "openai": {
        "api_key_var": "OPENAI_API_KEY",
        "model_var": "OPENAI_MODEL",
        "base_url_var": "OPENAI_API_BASE", 
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "provider_class": OpenAIProvider,
        "max_retries": 3,
        "timeout": 30,
        "confidence_score": 0.85
    },
    "local": {
        "api_key_var": None,  # No API key needed
        "model_var": "LOCAL_LLM_MODEL",
        "base_url_var": "LOCAL_LLM_URL",
        "default_base_url": "http://192.168.0.47:5000",
        "default_model": "Phi-3-mini-128k-instruct-onnx",
        "provider_class": LocalProvider,
        "max_retries": 1,
        "timeout": 60,
        "confidence_score": 0.6
    },
    "gemini": {
        "api_key_var": "GEMINI_API_KEY",
        "model_var": "GEMINI_MODEL",
        "base_url_var": "GEMINI_API_BASE",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-2.5-pro",
        "provider_class": LocalProvider,  # Placeholder - needs implementation
        "max_retries": 3,
        "timeout": 30,
        "confidence_score": 0.8
    },
    "huggingface": {
        "api_key_var": "HUGGINGFACE_API_KEY",
        "model_var": "HUGGINGFACE_MODEL",
        "base_url_var": "HUGGINGFACE_API_URL",
        "default_base_url": "https://api-inference.huggingface.co/models",
        "default_model": "tiiuae/falcon-7b-instruct",
        "provider_class": LocalProvider,  # Placeholder - needs implementation
        "max_retries": 3,
        "timeout": 30,
        "confidence_score": 0.7
    },
    "openrouter": {
        "api_key_var": "OPENROUTER_API_KEY",
        "model_var": "OPENROUTER_MODEL",
        "base_url_var": "OPENROUTER_API_BASE",
        "default_base_url": "https://openrouter.ai/api/v1",
        "default_model": "openrouter-default",
        "provider_class": OpenAIProvider,  # Compatible API
        "max_retries": 3,
        "timeout": 30,
        "confidence_score": 0.8
    },
    "anthropic": {
        "api_key_var": "ANTHROPIC_API_KEY",
        "model_var": "ANTHROPIC_MODEL",
        "base_url_var": "ANTHROPIC_API_BASE",
        "default_base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-opus",
        "provider_class": OpenAIProvider,  # Placeholder - would need custom class for production
        "max_retries": 3,
        "timeout": 30,
        "confidence_score": 0.85
    }
}


class ProviderRegistry:
    """Central registry for managing LLM providers"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self._providers: Dict[str, BaseLLMProvider] = {}
        self._fallback_chain: List[str] = []
        
        # Initialize providers from environment using schema
        self._initialize_from_environment()
    
    def _initialize_from_environment(self):
        """Initialize providers based on environment configuration using schema"""
        # Get primary provider from environment
        primary_provider = os.getenv("CHAT_PROVIDER", "local")
        
        # Validate that primary_provider is in schema
        if primary_provider not in PROVIDER_SCHEMA:
            valid_options = list(PROVIDER_SCHEMA.keys())
            self.logger.error(
                f"❌ Invalid CHAT_PROVIDER: '{primary_provider}'. "
                f"Valid options: {valid_options}. Defaulting to 'local'"
            )
            primary_provider = "local"
        
        # Initialize all providers defined in schema
        for provider_name, schema in PROVIDER_SCHEMA.items():
            try:
                config = self._create_provider_config(provider_name, schema)
                if config:
                    self._initialize_provider(provider_name, config)
            except Exception as e:
                self.logger.warning(f"Failed to initialize provider {provider_name}: {e}")
        
        # Set up fallback chain with primary first
        self._setup_fallback_chain(primary_provider)
    
    def _create_provider_config(self, provider_name: str, schema: Dict) -> Optional[ProviderConfig]:
        """Create provider configuration from schema and environment variables"""
        
        # Check if API key is required and available
        api_key_var = schema.get("api_key_var")
        api_key = None
        if api_key_var:
            api_key = os.getenv(api_key_var)
            if not api_key:
                # Skip providers without required API keys
                return None
        
        # Get configuration values from environment or defaults
        model = os.getenv(schema["model_var"], schema["default_model"])
        base_url = os.getenv(schema.get("base_url_var", ""), schema["default_base_url"])
        
        return ProviderConfig(
            name=provider_name,
            api_key=api_key,
            base_url=base_url,
            models=[model],
            max_retries=schema["max_retries"],
            timeout=schema["timeout"],
            confidence_score=schema["confidence_score"]
        )
    
    def _initialize_provider(self, name: str, config: ProviderConfig):
        """Initialize a single provider using schema"""
        if name not in PROVIDER_SCHEMA:
            self.logger.warning(f"Unknown provider in schema: {name}")
            return
        
        schema = PROVIDER_SCHEMA[name]
        provider_class = schema["provider_class"]
        
        try:
            provider = provider_class(config)
            
            if provider.is_available():
                self._providers[name] = provider
                self.logger.info(f"✅ Provider '{name}' initialized successfully")
            else:
                self.logger.warning(f"❌ Provider '{name}' not available (missing config)")
        except Exception as e:
            self.logger.error(f"❌ Error creating provider '{name}': {e}")
    
    def _setup_fallback_chain(self, primary_provider: str):
        """Set up the provider fallback chain"""
        # Start with primary provider
        chain = [primary_provider] if primary_provider in self._providers else []
        
        # Add other available providers as fallbacks
        fallback_order = ["fireworks", "openai", "local"]
        for provider in fallback_order:
            if provider != primary_provider and provider in self._providers:
                chain.append(provider)
        
        self._fallback_chain = chain
        self.logger.info(f"Provider fallback chain: {' -> '.join(chain)}")
    
    def register_provider(self, name: str, provider_class: Type[BaseLLMProvider]):
        """Register a custom provider class"""
        self._provider_classes[name] = provider_class
        self.logger.info(f"Registered custom provider class: {name}")
    
    def get_provider(self, name: str) -> Optional[BaseLLMProvider]:
        """Get a specific provider by name"""
        return self._providers.get(name)
    
    def get_available_providers(self) -> List[str]:
        """Get list of available provider names"""
        return list(self._providers.keys())
    
    def get_all_provider_names(self) -> List[str]:
        """Get list of all provider names defined in schema"""
        return list(PROVIDER_SCHEMA.keys())
    
    def get_fallback_chain(self) -> List[str]:
        """Get the current fallback chain"""
        return self._fallback_chain.copy()
    
    async def route_request(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        confidence_threshold: float = 0.8,
        **kwargs
    ) -> LLMResponse:
        """
        Route request through the fallback chain until success
        
        Args:
            prompt: Input prompt
            model: Specific model to use (optional)
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            confidence_threshold: Minimum confidence threshold
            **kwargs: Additional parameters
            
        Returns:
            LLMResponse from successful provider
            
        Raises:
            Exception: If all providers fail
        """
        last_error = None
        
        for provider_name in self._fallback_chain:
            provider = self._providers.get(provider_name)
            if not provider:
                continue
            
            try:
                self.logger.info(f"Trying provider: {provider_name}")
                
                response = await provider.generate(
                    prompt=prompt,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    **kwargs
                )
                
                # Check confidence threshold
                if response.confidence >= confidence_threshold:
                    self.logger.info(
                        f"✅ Success with {provider_name} "
                        f"(confidence: {response.confidence:.2f})"
                    )
                    return response
                else:
                    self.logger.warning(
                        f"⚠️ Low confidence from {provider_name} "
                        f"({response.confidence:.2f} < {confidence_threshold})"
                    )
                    continue
                    
            except Exception as e:
                self.logger.warning(f"❌ Provider {provider_name} failed: {e}")
                last_error = e
                continue
        
        # All providers failed
        error_msg = f"All providers failed. Last error: {last_error}"
        self.logger.error(error_msg)
        raise Exception(error_msg)
    
    def get_provider_status(self) -> Dict[str, Dict[str, any]]:
        """Get status information for all providers"""
        status = {}
        
        for name, provider in self._providers.items():
            status[name] = {
                "available": provider.is_available(),
                "models": provider.get_supported_models(),
                "confidence_score": provider.config.confidence_score,
                "in_fallback_chain": name in self._fallback_chain
            }
        
        return status


# Global registry instance
_registry = None


def get_registry() -> ProviderRegistry:
    """Get the global provider registry instance"""
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry


def reset_registry():
    """Reset the global registry (mainly for testing)"""
    global _registry
    _registry = None


def get_valid_provider_names() -> List[str]:
    """Get list of valid provider names for CHAT_PROVIDER"""
    return list(PROVIDER_SCHEMA.keys())


def print_provider_options():
    """Print all valid provider options with descriptions"""
    print("Valid CHAT_PROVIDER options:")
    for name, schema in PROVIDER_SCHEMA.items():
        provider_class = schema["provider_class"].__name__
        default_model = schema["default_model"]
        print(f'  "{name}" - {provider_class} ({default_model})')
    print(f"\nExample: CHAT_PROVIDER=\"fireworks\"")