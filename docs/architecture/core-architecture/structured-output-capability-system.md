# Structured Output Capability System

## Overview

The Structured Output Capability System provides a **provider-agnostic** approach to handling structured output across all LLM providers (OpenAI, Groq, Anthropic, Gemini, Cohere, local models, etc.).

Instead of hardcoding provider-specific logic throughout the codebase, this system centralizes capability detection and automatically adjusts prompts and API parameters based on what each provider/model supports.

## Architecture

### Components

1. **`structured_output_capability.py`** - Core capability system
   - `StructuredOutputCapability` enum (STRICT, BEST_EFFORT, FUNCTION_CALLING, NONE)
   - `StructuredOutputMode` enum (JSON_SCHEMA_STRICT, JSON_OBJECT, FUNCTION_CALLING, PROMPT_ONLY)
   - `StructuredOutputStrategy` dataclass
   - `get_capability_for_provider_and_model()` - Centralized capability registry
   - `create_strategy_for_capability()` - Strategy factory

2. **`base.py`** - Base provider class with capability methods
   - `get_structured_output_capability()` - Detects capability for model
   - `get_structured_output_strategy()` - Creates strategy for schema

3. **`milestone_engine.py`** - Consumer of capability system
   - Uses `get_structured_output_strategy()` to determine approach
   - Conditionally includes schema in prompt
   - Uses strategy-determined `response_format`

### Capability Levels

| Capability | Description | Example Models | API Support |
|------------|-------------|----------------|-------------|
| **STRICT** | Guaranteed schema adherence with `json_schema` and `strict:true` | OpenAI GPT-4o, Groq gpt-oss-20b/120b | `response_format: {type: "json_schema", json_schema: {...}}` |
| **BEST_EFFORT** | JSON object mode, schema must be in prompt | Groq Llama-3.3, Mixtral, most local models | `response_format: {type: "json_object"}` |
| **FUNCTION_CALLING** | Uses tool/function calling for structured output | Anthropic Claude, some local models | `tools: [...]` parameter |
| **NONE** | No API support, schema only in prompt | Legacy/small models | Prompt engineering only |

### How It Works

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Client Code                                  │
│                  (milestone_engine.py)                               │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             │ get_structured_output_strategy(schema)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      BaseLLMProvider                                 │
│                         (base.py)                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ get_structured_output_capability()                            │  │
│  │   └─> get_capability_for_provider_and_model()                │  │
│  │         (centralized registry)                                │  │
│  │                                                                │  │
│  │ get_structured_output_strategy()                              │  │
│  │   └─> create_strategy_for_capability()                       │  │
│  └──────────────────────────────────────────────────────────────┘  │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             │ Returns: StructuredOutputStrategy
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   StructuredOutputStrategy                           │
│                                                                      │
│  - capability: StructuredOutputCapability                           │
│  - mode: StructuredOutputMode                                       │
│  - include_schema_in_prompt: bool                                   │
│  - response_format: Optional[Dict]                                  │
│  - extra_config: Dict                                               │
└─────────────────────────────────────────────────────────────────────┘
```

## Usage Example

### Before (Hardcoded Provider Logic)

```python
# milestone_engine.py - OLD APPROACH
response_format = create_response_format_json_schema(schema_model)

# PROBLEM: Always includes schema in prompt, even for STRICT-capable models
schema_json = json.dumps(schema_model.model_json_schema(), indent=2)
prompt_with_schema = f"{prompt}\n\nSchema: {schema_json}"

response = await self.llm_provider.generate(
    prompt=prompt_with_schema,
    response_format=response_format,
)
```

```python
# groq_provider.py - OLD APPROACH
# PROBLEM: Hardcoded model list
STRICT_JSON_SCHEMA_MODELS = {
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
}

if effective_model not in STRICT_JSON_SCHEMA_MODELS:
    response_format = {"type": "json_object"}
```

### After (Capability System)

```python
# milestone_engine.py - NEW APPROACH
schema = schema_model.model_json_schema()
strategy = self.llm_provider.get_structured_output_strategy(schema)

# Conditionally include schema based on provider capability
if strategy.include_schema_in_prompt:
    schema_json = json.dumps(schema, indent=2)
    final_prompt = f"{prompt}\n\nSchema: {schema_json}"
else:
    final_prompt = prompt

# Apply strategy-specific parameters
if strategy.mode == StructuredOutputMode.FUNCTION_CALLING:
    # Use tools/function calling (Anthropic, etc.)
    from faultmaven.utils.schema_converter import pydantic_to_openai_tools
    generate_params = {
        "prompt": final_prompt,
        "tools": pydantic_to_openai_tools(schema_model),
        "tool_choice": "required"
    }
else:
    # Use response_format (OpenAI, Groq, etc.)
    generate_params = {
        "prompt": final_prompt,
        "response_format": strategy.response_format
    }

response = await self.llm_provider.generate(**generate_params)

# For function calling, extract from tool_calls
if strategy.mode == StructuredOutputMode.FUNCTION_CALLING:
    content = response.tool_calls[0].function.get("arguments", "{}")
else:
    content = response.content
```

```python
# groq_provider.py - NEW APPROACH
if "response_format" in kwargs:
    response_format = kwargs.pop("response_format")

    if response_format.get("type") == "json_schema":
        capability = self.get_structured_output_capability(effective_model)

        if capability != StructuredOutputCapability.STRICT:
            self.logger.warning(
                f"Model {effective_model} has capability={capability.value}, "
                f"falling back to json_object mode"
            )
            response_format = {"type": "json_object"}
```

## Benefits

### 1. No Hardcoded Provider Logic
- Adding new providers: Update capability registry only
- Adding new models: Update capability registry only
- No scattered provider checks throughout codebase

### 2. Graceful Degradation
- Automatically falls back to best available mode
- Maintains functionality across all providers
- Consistent behavior regardless of provider

### 3. Maintainability
- Single source of truth for capabilities
- Easy to update as providers add features
- Clear separation of concerns

### 4. Testability
- Mock capability levels to test different scenarios
- Comprehensive unit tests for all capability levels
- Easy to verify behavior across providers

### 5. Performance Optimization
- STRICT-capable models: No schema in prompt (saves tokens)
- BEST_EFFORT models: Schema in prompt only when needed
- Optimal approach for each provider/model

## Adding Support for New Providers

### Step 1: Update Capability Registry

Edit `structured_output_capability.py`:

```python
def get_capability_for_provider_and_model(
    provider_name: str, model_name: str
) -> StructuredOutputCapability:
    """Centralized capability registry"""

    # ... existing providers ...

    # NEW PROVIDER
    elif provider_name == "new_provider":
        if "advanced-model" in model_name:
            return StructuredOutputCapability.STRICT
        return StructuredOutputCapability.BEST_EFFORT

    # Default fallback
    return StructuredOutputCapability.BEST_EFFORT
```

### Step 2: Implement Provider Class

Create `new_provider.py`:

```python
from .base import BaseLLMProvider

class NewProvider(BaseLLMProvider):
    @property
    def provider_name(self) -> str:
        return "new_provider"

    # Capability methods inherited from BaseLLMProvider - no code needed!
    # get_structured_output_capability() and get_structured_output_strategy()
    # automatically work via the centralized registry

    async def generate(self, prompt: str, **kwargs):
        # Handle response_format from strategy
        strategy = self.get_structured_output_strategy(schema)

        # Use strategy.response_format in API call
        response = await api_call(
            prompt=prompt,
            response_format=strategy.response_format
        )
        return response
```

### Step 3: Add Tests

Create `test_new_provider_capability.py`:

```python
def test_new_provider_advanced_model_strict():
    """New provider's advanced model should support STRICT"""
    capability = get_capability_for_provider_and_model(
        "new_provider", "advanced-model"
    )
    assert capability == StructuredOutputCapability.STRICT

def test_new_provider_basic_model_best_effort():
    """New provider's basic model should use BEST_EFFORT"""
    capability = get_capability_for_provider_and_model(
        "new_provider", "basic-model"
    )
    assert capability == StructuredOutputCapability.BEST_EFFORT
```

## Current Provider Support

| Provider | STRICT Models | BEST_EFFORT Models | FUNCTION_CALLING Models |
|----------|---------------|-------------------|------------------------|
| **OpenAI** | GPT-4o, GPT-4o-mini, GPT-4-turbo, GPT-4-2024, GPT-3.5-turbo-0125 | - | Older GPT-3.5 models |
| **Groq** | openai/gpt-oss-20b, openai/gpt-oss-120b | Llama-3.3, Mixtral, all other models | - |
| **Anthropic** | - | - | All Claude models |
| **Gemini** | Gemini 2.0, Gemini 1.5 | Gemini 1.0 and older | - |
| **Cohere** | - | All models | - |
| **Local** | - | Most models | Functionary, Hermes |

## Design Principles

### 1. Centralized Registry
All capability information lives in one place: `get_capability_for_provider_and_model()`

### 2. Provider Inheritance
Providers inherit capability methods from `BaseLLMProvider` - no per-provider implementation needed

### 3. Strategy Pattern
Different capability levels get different strategies with appropriate configuration

### 4. Separation of Concerns
- **Detection**: `get_capability_for_provider_and_model()`
- **Strategy**: `create_strategy_for_capability()`
- **Execution**: Consumer code (milestone_engine)

### 5. Fail-Safe Defaults
Unknown providers default to `BEST_EFFORT` - always functional, never broken

## Performance Considerations

### Token Usage by Capability Mode

Different capability modes have different token overhead:

| Mode | Token Overhead | Schema Location | Best For |
|------|----------------|-----------------|----------|
| **STRICT** | Low | In `response_format` (not counted as input) | OpenAI, Groq strict models |
| **BEST_EFFORT** | Medium | In prompt text (~5-10KB for complex schemas) | Most models |
| **FUNCTION_CALLING** | **High** | In `tools` parameter (counted as input) | Anthropic Claude |
| **NONE** | Medium | In prompt text only | Legacy models |

**⚠️ FUNCTION_CALLING Token Impact:**

- Tool definitions are included in **every request** as input tokens
- Complex schemas can add 5-15KB per request
- Monitor token usage carefully when using Anthropic with large schemas
- Consider schema simplification for high-volume applications

**Mitigation Strategies:**
```python
# 1. Simplify schemas for function calling
class SimplifiedResponse(BaseModel):
    """Use fewer fields for Anthropic to reduce token overhead"""
    summary: str  # Instead of 10 detailed fields
    data: dict    # Flexible structure if needed

# 2. Monitor token usage
response = await provider.generate(...)
logger.info(f"Tokens used: {response.tokens_used}")
if response.tokens_used > threshold:
    alert_high_token_usage()

# 3. Use caching where available
# Some providers cache tool definitions across requests
```

## Future Enhancements

### 1. Dynamic Capability Discovery
Query provider API for capabilities instead of hardcoded registry:
```python
async def get_model_capabilities(provider, model) -> StructuredOutputCapability:
    info = await provider.get_model_info(model)
    if info.supports_strict_json_schema:
        return StructuredOutputCapability.STRICT
    # ...
```

### 2. Capability Caching
Cache capability lookups to avoid repeated registry queries:
```python
@lru_cache(maxsize=256)
def get_capability_for_provider_and_model(provider, model):
    # ...
```

### 3. Runtime Capability Overrides
Allow users to override detected capabilities:
```python
provider.override_capability(model="llama-3.3", capability=STRICT)
```

### 4. Capability Metrics
Track capability usage for analytics:
```python
metrics.increment("structured_output.capability", tags={
    "provider": "groq",
    "model": "llama-3.3",
    "capability": "best_effort"
})
```

## References

- [Structured Output Guide](../../development/structured-output-guide.md)
- [OpenAI Structured Outputs](https://platform.openai.com/docs/guides/structured-outputs)
- [Groq Structured Outputs](https://console.groq.com/docs/structured-outputs)
- Source: `faultmaven/infrastructure/llm/structured_output_capability.py`
- Tests: `tests/unit/infrastructure/llm/test_structured_output_capability.py`
