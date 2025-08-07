# LLM Provider Documentation

This guide documents FaultMaven's **centralized provider registry system** with comprehensive multi-LLM support, including all 7 supported providers and instructions for adding new ones.

## Architecture Overview

FaultMaven's LLM system is built on **clean architecture principles** with:

- **Interface-Based Design**: All providers implement the `ILLMProvider` interface
- **Centralized Registry**: Single `PROVIDER_SCHEMA` manages all provider configurations
- **Automatic Fallback Chains**: Primary → Fireworks → OpenAI → Local (based on available API keys)
- **Dependency Injection**: LLM providers injected as `ILLMProvider` interface throughout the system
- **Health Monitoring**: Real-time provider availability and performance tracking
- **Zero-Configuration Setup**: Providers auto-initialize based on environment variables

## Currently Supported Providers (7 Total)

FaultMaven supports 7 LLM providers out of the box with automatic registration:

| Provider | Type | Best For | Auto-Fallback Priority |
|----------|------|----------|------------------------|
| **Fireworks AI** | Commercial | High-performance, cost-effective | ♥1st (after primary) |
| **OpenAI** | Commercial | Highest quality responses | ♥2nd (reliable fallback) |
| **Anthropic** | Commercial | Reasoning, code analysis | User-configurable primary |
| **Google Gemini** | Commercial | Multimodal, large context | User-configurable primary |
| **Hugging Face** | Open Source | Research, experimentation | User-configurable primary |
| **OpenRouter** | Aggregator | Multi-provider access | User-configurable primary |
| **Local** | Self-Hosted | Privacy, customization | ♥3rd (always available) |

### 1. Fireworks AI (Recommended)

**Best for**: Fast inference, cost-effective, open source models

```bash
# Environment Configuration
FIREWORKS_API_KEY="fw_your_api_key"
FIREWORKS_MODEL="accounts/fireworks/models/llama-v3p1-8b-instruct"
CHAT_PROVIDER="fireworks"
```

**Available Models**:
- `accounts/fireworks/models/llama-v3p1-8b-instruct` (Default)
- `accounts/fireworks/models/llama-v3p1-70b-instruct`
- `accounts/fireworks/models/mixtral-8x7b-instruct`

**Features**: High performance, competitive pricing, good for production workloads.

### 2. OpenAI

**Best for**: Highest quality responses, most reliable

```bash
# Environment Configuration  
OPENAI_API_KEY="sk-your_openai_key"
OPENAI_MODEL="gpt-4o"
CHAT_PROVIDER="openai"
```

**Available Models**:
- `gpt-4o` (Default, recommended)
- `gpt-4o-mini` (Faster, cheaper)
- `gpt-3.5-turbo` (Legacy)

**Features**: Best response quality, extensive capabilities, higher cost.

### 3. Anthropic (Claude)

**Best for**: Advanced reasoning, code analysis, complex problem solving

```bash
# Environment Configuration
ANTHROPIC_API_KEY="sk-ant-your_key"
ANTHROPIC_MODEL="claude-3-5-sonnet-20241022"  # Latest model (default)
CHAT_PROVIDER="anthropic"
```

**Available Models**:
- `claude-3-5-sonnet-20241022` (Default, latest with improved capabilities)
- `claude-3-5-sonnet-20240620` (Previous version)
- `claude-3-haiku-20240307` (Fastest, most cost-effective)
- `claude-3-opus-20240229` (Highest capability, most expensive)

**Features**: 
- Exceptional reasoning and analysis capabilities
- Large context windows (200K tokens)
- Strong code understanding and generation
- Enhanced safety and helpfulness
- Excellent for troubleshooting complex system issues

### 4. Google Gemini

**Best for**: Multimodal capabilities, Google ecosystem integration

```bash
# Environment Configuration
GEMINI_API_KEY="your_google_ai_key"
GEMINI_MODEL="gemini-1.5-pro"
CHAT_PROVIDER="gemini"
```

**Available Models**:
- `gemini-1.5-pro` (Default)
- `gemini-1.5-flash` (Faster, cheaper)
- `gemini-pro-vision` (Multimodal)

**Features**: Large context windows, multimodal input, competitive pricing.

### 5. Hugging Face

**Best for**: Open source models, research, experimentation

```bash
# Environment Configuration
HUGGINGFACE_API_KEY="hf_your_token"
HUGGINGFACE_MODEL="tiiuae/falcon-7b-instruct"
CHAT_PROVIDER="huggingface"
```

**Available Models**:
- `tiiuae/falcon-7b-instruct` (Default)
- `microsoft/DialoGPT-large`
- `meta-llama/Llama-2-7b-chat-hf`

**Features**: Access to thousands of open source models, free tier available.

### 6. OpenRouter

**Best for**: Access to multiple providers, cost optimization

```bash
# Environment Configuration
OPENROUTER_API_KEY="sk-or-your_key"
OPENROUTER_MODEL="anthropic/claude-3-sonnet"
CHAT_PROVIDER="openrouter"
```

**Available Models**:
- `anthropic/claude-3-sonnet`
- `openai/gpt-4`
- `meta-llama/llama-3-8b-instruct`

**Features**: One API for multiple providers, competitive pricing, provider flexibility.

### 7. Local (Self-Hosted)

**Best for**: Privacy, customization, cost control

```bash
# Environment Configuration (no API key needed)
LOCAL_LLM_URL="http://192.168.0.47:5000"
LOCAL_LLM_MODEL="Phi-3-mini-128k-instruct-onnx"
CHAT_PROVIDER="local"
```

**Supported Servers**:
- Ollama
- vLLM
- Text Generation Inference
- Custom OpenAI-compatible servers

**Features**: Complete privacy, customizable models, no external dependencies.

## Provider Selection Guide

### By Use Case

| Use Case | Primary Provider | Recommended Fallback Chain | Rationale |
|----------|------------------|----------------------------|----------|
| **Production SRE/DevOps** | `fireworks` | → `openai` → `local` | Speed + reliability + cost-effectiveness |
| **Enterprise Troubleshooting** | `anthropic` | → `openai` → `fireworks` | Advanced reasoning + enterprise support |
| **Development/Testing** | `local` | → `fireworks` → `openai` | Privacy + cost control + good fallbacks |
| **Research & Experimentation** | `huggingface` | → `gemini` → `fireworks` | Model variety + cutting-edge capabilities |
| **Multi-Provider Flexibility** | `openrouter` | → `anthropic` → `openai` | Provider diversity + unified billing |
| **Cost-Conscious** | `fireworks` | → `local` → `huggingface` | Low cost + local fallback |
| **Maximum Privacy** | `local` | → (none - local only) | Complete data control |

### Performance Characteristics

| Provider | Speed | Quality | Reliability | Cost | Context Window |
|----------|-------|---------|-------------|------|--------------|
| **Fireworks AI** | ★★★★★ | ★★★★☆ | ★★★★★ | ★★★★★ | 8K-32K |
| **OpenAI** | ★★★★☆ | ★★★★★ | ★★★★★ | ★★★☆☆ | 128K |
| **Anthropic** | ★★★☆☆ | ★★★★★ | ★★★★★ | ★★★☆☆ | 200K |
| **Google Gemini** | ★★★★☆ | ★★★★☆ | ★★★★☆ | ★★★★☆ | 2M |
| **HuggingFace** | ★★☆☆☆ | ★★★☆☆ | ★★★☆☆ | ★★★★★ | Varies |
| **OpenRouter** | ★★★★☆ | ★★★★☆ | ★★★★☆ | ★★★★☆ | Varies |
| **Local** | ★★★☆☆* | ★★★☆☆* | ★★★★★ | ★★★★★ | Model-dependent |

*Local performance depends on hardware configuration

## Centralized Provider Registry System

FaultMaven's provider system is built around a **centralized registry** that implements clean architecture principles with interface-based design.

### Architecture Components

```python
# Interface Definition (models/interfaces.py)
class ILLMProvider(ABC):
    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> LLMResponse: ...
    
    @abstractmethod
    def is_available(self) -> bool: ...

# Registry Implementation (infrastructure/llm/providers/registry.py)
class ProviderRegistry:
    def __init__(self):
        self._initialize_from_environment()  # Auto-discovery
        
# Global Access
registry = get_registry()  # Singleton instance

# Dependency Injection Integration
container.get_llm_provider()  # Returns ILLMProvider implementation
```

### Data-Driven Provider Schema

All providers are defined in a single `PROVIDER_SCHEMA` configuration:

```python
PROVIDER_SCHEMA = {
    "anthropic": {
        "api_key_var": "ANTHROPIC_API_KEY",
        "model_var": "ANTHROPIC_MODEL", 
        "default_model": "claude-3-5-sonnet-20241022",
        "provider_class": AnthropicProvider,  # Implements ILLMProvider
        "confidence_score": 0.85,
        "max_retries": 3,
        "timeout": 30
    },
    # ... 6 other providers
}
```

### Intelligent Fallback Chain System

The registry implements sophisticated fallback logic with confidence scoring:

```python
# Automatic fallback chain construction
class ProviderRegistry:
    def _setup_fallback_chain(self, primary_provider: str):
        # Primary provider first
        chain = [primary_provider] if primary_provider in self._providers else []
        
        # Add high-reliability fallbacks
        fallback_order = ["fireworks", "openai", "local"]
        for provider in fallback_order:
            if provider != primary_provider and provider in self._providers:
                chain.append(provider)
                
        self._fallback_chain = chain
```

**Fallback Decision Logic**:
1. **Primary Provider**: User-configured via `CHAT_PROVIDER`
2. **First Fallback**: Fireworks AI (high performance + reliability)
3. **Second Fallback**: OpenAI (maximum reliability)
4. **Final Fallback**: Local (always available, no external dependency)

**Confidence-Based Routing**:
```python
async def route_request(self, prompt: str, confidence_threshold: float = 0.8):
    for provider_name in self._fallback_chain:
        try:
            response = await provider.generate(prompt)
            if response.confidence >= confidence_threshold:
                return response  # Success
        except Exception:
            continue  # Try next provider
    raise Exception("All providers failed")
```

**Real-World Fallback Examples**:
- **Enterprise**: `anthropic` → `openai` → `fireworks` → `local`
- **Cost-Optimized**: `fireworks` → `local` → `openai`
- **Research**: `huggingface` → `gemini` → `fireworks` → `local`
- **Privacy-First**: `local` → (no fallback - local only)

### Comprehensive Health Monitoring

The registry provides real-time health monitoring integrated with the dependency injection system:

```python
# Health Monitoring Integration
from faultmaven.container import container
from faultmaven.infrastructure.llm.providers.registry import get_registry

# Container-level health check
health = container.health_check()
print(f"Overall Status: {health['status']}")  # healthy | degraded | not_initialized

# Provider-specific status
registry = get_registry()
status = registry.get_provider_status()

for name, info in status.items():
    availability = "✅" if info['available'] else "❌"
    models = ", ".join(info['models'][:2])  # Show first 2 models
    confidence = info['confidence_score']
    in_chain = "⚡" if info['in_fallback_chain'] else "⏸️"
    
    print(f"{availability} {name:<12} | Models: {models:<30} | Confidence: {confidence} | {in_chain}")
```

**Example Health Output**:
```
Provider Health Status:
✅ fireworks    | Models: llama-v3p1-8b-instruct, llama-v3p1-70b | Confidence: 0.9  | ⚡
✅ openai       | Models: gpt-4o, gpt-4o-mini                    | Confidence: 0.85 | ⚡
✅ anthropic    | Models: claude-3-5-sonnet-20241022, claude-3-h | Confidence: 0.85 | ⚡
❌ gemini       | Models: gemini-1.5-pro, gemini-1.5-flash       | Confidence: 0.8  | ⏸️
✅ local        | Models: Phi-3-mini-128k-instruct-onnx           | Confidence: 0.6  | ⚡

Fallback Chain: anthropic → fireworks → openai → local
```

**HTTP Health Endpoint**:
```bash
# Check via API
curl http://localhost:8000/health/dependencies

# Response includes provider status
{
  "status": "healthy",
  "components": {
    "llm_provider": true,
    "provider_count": 4,
    "fallback_chain_length": 4
  }
}
```

### Zero-Configuration Auto-Discovery

Providers auto-initialize based on environment variables with **zero manual configuration**:

```bash
# Environment-Based Auto-Discovery
# Set any combination - system automatically detects and configures

FIREWORKS_API_KEY="fw_your_key"           # ✅ Fireworks auto-enabled
OPENAI_API_KEY="sk_your_openai_key"       # ✅ OpenAI auto-enabled
ANTHROPIC_API_KEY="sk-ant-your_key"       # ✅ Anthropic auto-enabled
GEMINI_API_KEY="your_google_ai_key"       # ✅ Gemini auto-enabled
# HUGGINGFACE_API_KEY not set             # ❌ HuggingFace disabled
# OPENROUTER_API_KEY not set              # ❌ OpenRouter disabled
LOCAL_LLM_URL="http://localhost:11434"    # ✅ Local auto-enabled (no API key needed)

# Primary provider selection
CHAT_PROVIDER="anthropic"  # Creates: anthropic → fireworks → openai → local
```

**Auto-Discovery Process**:
1. **Schema Scanning**: System scans `PROVIDER_SCHEMA` for all 7 providers
2. **API Key Detection**: Checks environment for each provider's `api_key_var`
3. **Provider Initialization**: Creates `ProviderConfig` and instantiates provider class
4. **Availability Testing**: Calls `provider.is_available()` to verify configuration
5. **Chain Construction**: Builds fallback chain with primary first, then reliability order
6. **Health Monitoring**: Continuous monitoring of all initialized providers

**Example Initialization Log**:
```
✅ Provider 'anthropic' initialized successfully
✅ Provider 'fireworks' initialized successfully  
✅ Provider 'openai' initialized successfully
❌ Provider 'gemini' not available (missing API key)
✅ Provider 'local' initialized successfully
Provider fallback chain: anthropic → fireworks → openai → local
```

### Provider Discovery

List all available providers:

```python
from faultmaven.infrastructure.llm.providers.registry import get_registry

registry = get_registry()

# All configured providers
print("Available:", registry.get_available_providers())

# All possible providers (from schema)  
print("All options:", registry.get_all_provider_names())

# Current fallback chain
print("Fallback chain:", registry.get_fallback_chain())
```

## Adding New Providers

The centralized registry system makes adding providers extremely simple through **data-driven configuration**.

### Quick Add (Schema-Only Configuration)

### Step 1: Add to Provider Schema

Edit `faultmaven/infrastructure/llm/providers/registry.py` and add your provider to `PROVIDER_SCHEMA`. For **OpenAI-compatible providers**, you can reuse existing classes:

```python
PROVIDER_SCHEMA = {
    # ... existing 7 providers ...
    
    # Example: Adding Together AI (OpenAI-compatible)
    "together": {
        "api_key_var": "TOGETHER_API_KEY",
        "model_var": "TOGETHER_MODEL",
        "base_url_var": "TOGETHER_API_BASE",
        "default_base_url": "https://api.together.xyz/v1",
        "default_model": "meta-llama/Llama-2-70b-chat-hf",
        "provider_class": OpenAIProvider,  # Reuse existing OpenAI class
        "max_retries": 3,
        "timeout": 30,
        "confidence_score": 0.8
    },
    
    # Example: Adding Cohere (needs custom provider class)
    "cohere": {
        "api_key_var": "COHERE_API_KEY",
        "model_var": "COHERE_MODEL", 
        "base_url_var": "COHERE_API_BASE",
        "default_base_url": "https://api.cohere.ai/v1",
        "default_model": "command-r-plus",
        "provider_class": CohereProvider,  # New custom class needed
        "max_retries": 3,
        "timeout": 30,
        "confidence_score": 0.8
    }
}
```

### Step 2: Environment Configuration (Auto-Discovery)

Once added to the schema, users can immediately use the new provider:

```env
# Together AI Configuration
TOGETHER_API_KEY="your-together-api-key"
TOGETHER_MODEL="meta-llama/Llama-2-70b-chat-hf"
CHAT_PROVIDER="together"  # Set as primary

# Multiple providers for fallback
FIREWORKS_API_KEY="fw_backup_key"  # Automatic fallback
OPENAI_API_KEY="sk_backup_key"     # Second fallback

# Result: together → fireworks → openai → local
```

**That's it!** The new provider:
1. ✅ Auto-initializes on application start
2. ✅ Integrates with dependency injection system
3. ✅ Appears in health monitoring
4. ✅ Participates in fallback chains  
5. ✅ Works with all existing FaultMaven features

## Custom Provider Implementation

**When needed**: If existing provider classes (OpenAI, Local, etc.) don't work with your API format.

### Interface Compliance Requirements

All custom providers **must implement** the `ILLMProvider` interface:

### Step 1: Create Provider Class

**Interface Requirements**: All providers must implement `ILLMProvider` from `models/interfaces.py`:

```python
# models/interfaces.py (existing interface definition)
class ILLMProvider(ABC):
    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> LLMResponse:
        """Generate response from LLM"""
        
    @abstractmethod
    def is_available(self) -> bool:
        """Check if provider is properly configured"""
        
    @abstractmethod
    def get_supported_models(self) -> List[str]:
        """Get list of supported models"""
```

Create `faultmaven/infrastructure/llm/providers/cohere_provider.py`:

```python
"""
Cohere LLM provider implementation.
Implements ILLMProvider interface for dependency injection compatibility.
"""

import aiohttp
from typing import List, Optional

from faultmaven.models.interfaces import ILLMProvider, LLMResponse
from .base import BaseLLMProvider, ProviderConfig


class CohereProvider(BaseLLMProvider, ILLMProvider):
    """Cohere LLM provider implementing ILLMProvider interface"""
    
    @property
    def provider_name(self) -> str:
        return "cohere"
    
    def is_available(self) -> bool:
        """Interface method: Check if provider is properly configured"""
        return bool(
            self.config.api_key and
            self.config.base_url and
            self.config.models
        )
    
    def get_supported_models(self) -> List[str]:
        """Interface method: Get list of supported models"""
        return self.config.models.copy()
    
    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        **kwargs
    ) -> LLMResponse:
        """Interface method: Generate response using Cohere API"""
        
        self._start_timing()
        
        # Get effective model
        effective_model = self.get_effective_model(model)
        
        # Cohere-specific API format
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": effective_model,
            "prompt": prompt,  # Cohere uses 'prompt' not 'messages'
            "max_tokens": max_tokens,
            "temperature": temperature,
            **kwargs
        }
        
        # Make API request
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.config.base_url}/generate",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout),
            ) as response:
                
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(
                        f"Cohere API error {response.status}: {error_text}"
                    )
                
                data = await response.json()
                
                # Extract Cohere response format
                content = data["generations"][0]["text"]
                content = self._validate_response_content(content)
                
                # Calculate usage metrics
                tokens_used = data.get("meta", {}).get("billed_units", {}).get("input_tokens", 0)
                tokens_used += data.get("meta", {}).get("billed_units", {}).get("output_tokens", 0)
                
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

### Step 2: Register with Schema and Container

**Import and Schema Registration** in `faultmaven/infrastructure/llm/providers/registry.py`:

```python
# Import new provider class
from .cohere_provider import CohereProvider

# Add to centralized schema
PROVIDER_SCHEMA = {
    # ... existing 7 providers ...
    
    "cohere": {
        "api_key_var": "COHERE_API_KEY",
        "model_var": "COHERE_MODEL",
        "base_url_var": "COHERE_API_BASE",
        "default_base_url": "https://api.cohere.ai/v1",
        "default_model": "command-r-plus",
        "provider_class": CohereProvider,  # Custom class implementing ILLMProvider
        "max_retries": 3,
        "timeout": 30,
        "confidence_score": 0.8
    }
}
```

**Dependency Injection Integration**: The registry automatically integrates with the DI container:

```python
# container.py automatically uses the registry
def _create_infrastructure_layer(self):
    # LLMRouter automatically includes all registry providers
    self.llm_provider: ILLMProvider = LLMRouter()
    
# Services receive providers through interface
class AgentService:
    def __init__(self, llm_provider: ILLMProvider, ...):
        self.llm_provider = llm_provider  # Works with any ILLMProvider implementation
```

### Step 3: Package Integration

**Update Package Exports** in `faultmaven/infrastructure/llm/providers/__init__.py`:

```python
from .cohere_provider import CohereProvider

__all__ = [
    # ... existing 7 providers ...
    "CohereProvider",
]
```

**Automatic System Integration**: Once registered, the new provider:

1. ✅ **Auto-Discovery**: Initializes based on `COHERE_API_KEY` environment variable
2. ✅ **Interface Compliance**: Implements `ILLMProvider` for DI container
3. ✅ **Health Monitoring**: Included in system health checks
4. ✅ **Fallback Integration**: Participates in automatic fallback chains
5. ✅ **Service Integration**: Works with all existing services (AgentService, etc.)
6. ✅ **Testing Support**: Can be mocked through `ILLMProvider` interface

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

### Registry Testing

```bash
# Test provider registration
source .venv/bin/activate
python -c "
from faultmaven.infrastructure.llm.providers.registry import get_registry
from faultmaven.container import container

registry = get_registry()
print('All providers in schema:', registry.get_all_provider_names())
print('Available providers:', registry.get_available_providers())
print('Fallback chain:', registry.get_fallback_chain())
print()

# Test dependency injection integration
llm_provider = container.get_llm_provider()
print(f'DI Container LLM Provider: {type(llm_provider).__name__}')

# Test health monitoring
health = container.health_check()
print(f'Container Health: {health["status"]}')
"
```

### Interface Compliance Testing

```python
# Test interface implementation
import pytest
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.infrastructure.llm.providers.cohere_provider import CohereProvider

def test_cohere_provider_implements_interface():
    """Test that CohereProvider implements ILLMProvider interface"""
    provider_config = ProviderConfig(
        name="cohere",
        api_key="test-key",
        base_url="https://api.cohere.ai/v1",
        models=["command-r-plus"]
    )
    
    provider = CohereProvider(provider_config)
    
    # Verify interface implementation
    assert isinstance(provider, ILLMProvider)
    assert hasattr(provider, 'generate')
    assert hasattr(provider, 'is_available')
    assert hasattr(provider, 'get_supported_models')

@pytest.mark.asyncio
async def test_cohere_provider_generates_response():
    """Test provider generates valid LLMResponse"""
    # Mock implementation test
    ...
```

### End-to-End Testing

```bash
# Test with real API key
export COHERE_API_KEY="your-test-key"
export CHAT_PROVIDER="cohere"

# Start application
./run_faultmaven.sh

# Test via API
curl -X POST http://localhost:8000/api/v1/query/troubleshoot \
  -H "Content-Type: application/json" \
  -d '{"query": "Test troubleshooting query", "session_id": "test-session"}'

# Should use Cohere provider as primary with fallback chain
```

## Provider Implementation Requirements

### Interface Compliance (Required)

All custom providers **must implement** the `ILLMProvider` interface:

```python
from faultmaven.models.interfaces import ILLMProvider, LLMResponse
from abc import ABC, abstractmethod

class ILLMProvider(ABC):
    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> LLMResponse:
        """Generate response from LLM - REQUIRED"""
        
    @abstractmethod  
    def is_available(self) -> bool:
        """Check if provider is properly configured - REQUIRED"""
        
    @abstractmethod
    def get_supported_models(self) -> List[str]:
        """Get list of supported models - REQUIRED"""
```

### Inheritance Pattern (Recommended)

For consistency, inherit from `BaseLLMProvider` which provides common functionality:

```python
class CustomProvider(BaseLLMProvider, ILLMProvider):
    """Custom provider implementing interface with base functionality"""
    
    @property
    def provider_name(self) -> str:
        return "custom"  # Unique identifier
    
    # Implement required interface methods
    def is_available(self) -> bool: ...
    def get_supported_models(self) -> List[str]: ...
    async def generate(self, prompt: str, **kwargs) -> LLMResponse: ...
    
    # BaseLLMProvider provides:
    # - Timing utilities (_start_timing, _get_response_time_ms)
    # - Model selection (get_effective_model)
    # - Response validation (_validate_response_content)
    # - Configuration management (self.config)
```

### Dependency Injection Integration

Providers must work with the DI container:

```python
# Your provider will be used like this:
from faultmaven.container import container

# Container resolves to ILLMProvider interface
llm_provider: ILLMProvider = container.get_llm_provider()

# Services receive your provider through interface injection
service = AgentService(
    llm_provider=llm_provider,  # Your custom provider
    tools=tools,
    tracer=tracer,
    sanitizer=sanitizer
)
```

**Required Methods Summary**:
- ✅ `async def generate()` - Core LLM generation (returns `LLMResponse`)
- ✅ `def is_available()` - Configuration validation (returns `bool`)
- ✅ `def get_supported_models()` - Model listing (returns `List[str]`)
- ✅ `property provider_name` - Unique identifier (returns `str`)

## Examples of Compatible APIs

Many providers can reuse existing classes:

- **OpenAI-compatible**: OpenRouter, Together AI, many local servers
- **Use**: `OpenAIProvider`

- **Local servers**: Ollama, vLLM, Text Generation Inference
- **Use**: `LocalProvider` 

- **Custom APIs**: Anthropic, Cohere, AI21
- **Need**: Custom provider class

## Troubleshooting Guide

### Health Monitoring Commands

```bash
# Check overall system health
curl http://localhost:8000/health/dependencies

# Check provider registry status  
python -c "
from faultmaven.infrastructure.llm.providers.registry import get_registry
from faultmaven.container import container

registry = get_registry()
status = registry.get_provider_status()

print('=== Provider Status ===')
for name, info in status.items():
    status_icon = '✅' if info['available'] else '❌'
    chain_icon = '⚡' if info['in_fallback_chain'] else '⏸️'
    print(f'{status_icon} {name:<12} | Confidence: {info["confidence_score"]} | {chain_icon}')

print(f'\nFallback Chain: {" → ".join(registry.get_fallback_chain())}')
print(f'Container Health: {container.health_check()["status"]}')
"
```

### Common Provider Issues

### Model Name Format Issues

Each provider has specific model naming conventions:

```bash
# Provider-Specific Model Formats
FIREWORKS_MODEL="accounts/fireworks/models/llama-v3p1-8b-instruct"  # Full path required
OPENAI_MODEL="gpt-4o"                                              # Simple name
ANTHROPIC_MODEL="claude-3-5-sonnet-20241022"                      # Version-specific
GEMINI_MODEL="gemini-1.5-pro"                                     # Product-version
HUGGINGFACE_MODEL="tiiuae/falcon-7b-instruct"                    # org/model format
OPENROUTER_MODEL="anthropic/claude-3-sonnet"                      # provider/model
LOCAL_LLM_MODEL="Phi-3-mini-128k-instruct-onnx"                  # Server-dependent
```

**Error**: `Model not found, inaccessible, and/or not deployed`  
**Fixes**:
1. Check provider's model catalog/documentation
2. Verify model name case sensitivity
3. Ensure model is available in your region/tier
4. Test with provider's default model first

### Authentication Problems

**Error**: `Invalid API key`, `401 Unauthorized`, or `403 Forbidden`  
**Fixes**:
1. **Verify API Key**: Copy-paste carefully, check for extra spaces
2. **Check Permissions**: Ensure key has LLM generation permissions
3. **Validate Format**: Each provider has different key formats:
   ```bash
   OPENAI_API_KEY="sk-..."           # OpenAI format
   ANTHROPIC_API_KEY="sk-ant-..."    # Anthropic format  
   FIREWORKS_API_KEY="fw_..."        # Fireworks format
   GEMINI_API_KEY="AIza..."          # Google format
   ```
4. **Test Separately**: Verify key works with provider's official tools
5. **Check Quotas**: Ensure account has remaining credits/quota

### Connection and Configuration Issues

**Error**: `Connection refused`, `404 Not Found`, or `Timeout`  
**Fixes**:

1. **Base URL Configuration**:
   ```bash
   # Default URLs (usually correct)
   OPENAI_API_BASE="https://api.openai.com/v1"      # Default
   ANTHROPIC_API_BASE="https://api.anthropic.com/v1" # Default
   
   # Custom URLs (for proxies, local servers)
   LOCAL_LLM_URL="http://localhost:11434"            # Ollama
   LOCAL_LLM_URL="http://192.168.0.47:5000"          # Custom server
   ```

2. **Network Issues**:
   - Verify internet connectivity
   - Check firewall/proxy settings
   - Test with `curl` to same URL

3. **Provider Health Check**:
   ```bash
   # Check provider status via health endpoint
   curl http://localhost:8000/health/dependencies
   
   # Look for provider availability
   python -c "from faultmaven.infrastructure.llm.providers.registry import get_registry; 
   print(get_registry().get_provider_status())"
   ```

4. **Local Server Issues**:
   - Ensure Ollama/vLLM server is running
   - Check server logs for errors
   - Verify model is loaded: `ollama list`

### Dependency Injection Issues

**Error**: `Service not available` or `Container not initialized`  
**Fixes**:
1. Check container health: `container.health_check()`
2. Verify interface implementation in custom providers
3. Ensure provider is properly registered in schema
4. Reset container state: `container.reset()` (in tests)

## Implementation Summary

### Quick Provider Addition (80% of Cases)

**For OpenAI-compatible APIs** (Together AI, Perplexity, etc.):
1. ✅ Add to `PROVIDER_SCHEMA` using `OpenAIProvider` class
2. ✅ Users set environment variables
3. ✅ **Done** - Auto-discovery handles the rest

### Custom Provider Implementation (20% of Cases)

**For unique APIs** (Cohere, AI21, etc.):
1. ✅ Create provider class implementing `ILLMProvider` interface
2. ✅ Add to `PROVIDER_SCHEMA` with custom class
3. ✅ Update package imports
4. ✅ **Done** - Full system integration automatic

### System Benefits

- **Zero Configuration**: Providers auto-initialize from environment
- **Clean Architecture**: Interface-based design enables easy testing
- **Dependency Injection**: Automatic integration with DI container
- **Health Monitoring**: Real-time provider status and performance
- **Intelligent Fallback**: Automatic failover chains based on reliability
- **Zero Downtime**: New providers available immediately without restarts

### Migration Path

Existing FaultMaven installations automatically benefit from new providers:
1. Pull latest code with provider additions
2. Add environment variables for desired providers
3. Restart application
4. New providers immediately available in fallback chain

**Total Supported**: **7 providers out-of-the-box** + unlimited custom providers through the extensible registry system.