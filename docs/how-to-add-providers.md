# How to Add New LLM Providers

This guide explains how to add new LLM providers to FaultMaven.

## Quick Add (Using Existing Provider Classes)

### Step 1: Add to Provider Schema

Edit `faultmaven/infrastructure/llm/providers/registry.py` and add your provider to `PROVIDER_SCHEMA`:

```python
PROVIDER_SCHEMA = {
    # ... existing providers ...
    
    "anthropic": {
        "api_key_var": "ANTHROPIC_API_KEY",
        "model_var": "ANTHROPIC_MODEL",
        "base_url_var": "ANTHROPIC_API_BASE",
        "default_base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-opus",
        "provider_class": OpenAIProvider,  # Use existing compatible class
        "max_retries": 3,
        "timeout": 30,
        "confidence_score": 0.85
    }
}
```

### Step 2: Update Environment Variables

Users can now add to their `.env`:

```env
# Anthropic Claude
ANTHROPIC_API_KEY="your-anthropic-api-key"
ANTHROPIC_MODEL="claude-3-opus"

# Set as primary provider
CHAT_PROVIDER="anthropic"
```

**That's it!** The new provider is automatically available.

## Custom Provider Implementation

If existing provider classes don't work, create a custom implementation:

### Step 1: Create Provider Class

Create `faultmaven/infrastructure/llm/providers/anthropic_provider.py`:

```python
"""
Anthropic Claude provider implementation.
"""

import aiohttp
from typing import List, Optional

from .base import BaseLLMProvider, LLMResponse, ProviderConfig


class AnthropicProvider(BaseLLMProvider):
    """Anthropic Claude LLM provider implementation"""
    
    @property
    def provider_name(self) -> str:
        return "anthropic"
    
    def is_available(self) -> bool:
        """Check if Anthropic provider is properly configured"""
        return bool(
            self.config.api_key and
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
        """Generate response using Anthropic API"""
        
        self._start_timing()
        
        # Get effective model
        effective_model = self.get_effective_model(model)
        
        # Prepare request (Anthropic-specific format)
        headers = {
            "x-api-key": self.config.api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01"
        }
        
        payload = {
            "model": effective_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        
        # Add any additional kwargs
        payload.update(kwargs)
        
        # Make request
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.config.base_url}/messages",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout),
            ) as response:
                
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(
                        f"Anthropic API error {response.status}: {error_text}"
                    )
                
                data = await response.json()
                
                # Extract response content (Anthropic-specific format)
                content = data["content"][0]["text"]
                content = self._validate_response_content(content)
                
                # Extract token usage
                usage = data.get("usage", {})
                tokens_used = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                
                response_time = self._get_response_time_ms()
                
                return LLMResponse(
                    content=content,
                    confidence=self.config.confidence_score,
                    provider=self.provider_name,
                    model=effective_model,
                    tokens_used=tokens_used,
                    response_time_ms=response_time,
                )
```

### Step 2: Import and Add to Schema

In `faultmaven/infrastructure/llm/providers/registry.py`:

```python
# Add import
from .anthropic_provider import AnthropicProvider

# Add to schema
PROVIDER_SCHEMA = {
    # ... existing providers ...
    
    "anthropic": {
        "api_key_var": "ANTHROPIC_API_KEY",
        "model_var": "ANTHROPIC_MODEL",
        "base_url_var": "ANTHROPIC_API_BASE",
        "default_base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-opus",
        "provider_class": AnthropicProvider,  # Use custom class
        "max_retries": 3,
        "timeout": 30,
        "confidence_score": 0.85
    }
}
```

### Step 3: Update Package Imports

In `faultmaven/infrastructure/llm/providers/__init__.py`:

```python
from .anthropic_provider import AnthropicProvider

__all__ = [
    # ... existing exports ...
    "AnthropicProvider",
]
```

## Schema Configuration Options

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `api_key_var` | No | Environment variable for API key | `"ANTHROPIC_API_KEY"` |
| `model_var` | Yes | Environment variable for model name | `"ANTHROPIC_MODEL"` |
| `base_url_var` | No | Environment variable for custom base URL | `"ANTHROPIC_API_BASE"` |
| `default_base_url` | Yes | Default API base URL | `"https://api.anthropic.com/v1"` |
| `default_model` | Yes | Default model if not specified | `"claude-3-opus"` |
| `provider_class` | Yes | Python class to handle requests | `AnthropicProvider` |
| `max_retries` | Yes | Number of retry attempts | `3` |
| `timeout` | Yes | Request timeout in seconds | `30` |
| `confidence_score` | Yes | Default confidence for responses | `0.85` |

## Testing New Providers

```bash
# Test the new provider
source .venv/bin/activate
python -c "
from faultmaven.infrastructure.llm.providers import get_registry
registry = get_registry()
print('Available providers:', registry.get_all_provider_names())
print('New provider available:', 'anthropic' in registry.get_all_provider_names())
"
```

## Provider Class Requirements

Custom providers must inherit from `BaseLLMProvider` and implement:

- `provider_name` (property) - Return unique provider name
- `is_available()` - Check if properly configured
- `get_supported_models()` - Return list of supported models  
- `generate()` - Main generation method (async)

## Examples of Compatible APIs

Many providers can reuse existing classes:

- **OpenAI-compatible**: OpenRouter, Together AI, many local servers
- **Use**: `OpenAIProvider`

- **Local servers**: Ollama, vLLM, Text Generation Inference
- **Use**: `LocalProvider` 

- **Custom APIs**: Anthropic, Cohere, AI21
- **Need**: Custom provider class

## Common Issues

### Model Name Format Errors

Different providers use different model name formats:

```bash
# Fireworks AI - requires full path
FIREWORKS_MODEL="accounts/fireworks/models/llama-v3p1-8b-instruct"

# OpenAI - simple names
OPENAI_MODEL="gpt-4o"

# Local - depends on server
LOCAL_LLM_MODEL="Phi-3-mini-128k-instruct-onnx"
```

**Error**: `Model not found, inaccessible, and/or not deployed`  
**Fix**: Check provider's API documentation for exact model names

### API Key Issues

**Error**: `Invalid API key` or `401 Unauthorized`  
**Fix**: Verify API key is correct and has proper permissions

### Base URL Issues

**Error**: `Connection refused` or `404 Not Found`  
**Fix**: Check if custom `base_url_var` is set correctly

## Summary

**For most providers**: Just add to `PROVIDER_SCHEMA` in `registry.py`  
**For custom APIs**: Create provider class + add to schema  
**Zero downtime**: New providers are available immediately