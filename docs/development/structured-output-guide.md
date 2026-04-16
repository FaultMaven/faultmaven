# Structured Output Guide: json_schema Mode

> **Design reference:** [Structured Output Capability System](../architecture/core-architecture/structured-output-capability-system.md) — why this system exists, template method pattern, capability levels.
> **This document:** implementation gotchas, strict-mode requirements, migration guidance.

## Overview

FaultMaven uses **json_schema mode** (OpenAI Structured Outputs) to enforce strict schema adherence when requesting structured data from LLMs. This guide documents critical implementation requirements and gotchas.

FaultMaven implements a **provider-agnostic capability detection system** that automatically detects each provider's structured output capabilities and adjusts the prompt and API parameters accordingly. This ensures optimal results across OpenAI, Groq, Anthropic, local models, and other providers without hardcoded provider-specific logic.

## Provider-Agnostic Capability System

### Architecture

FaultMaven uses a capability-based approach instead of hardcoded provider checks:

```python
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    get_capability_for_provider_and_model,
)

# Automatic capability detection
capability = provider.get_structured_output_capability(model)

# Returns one of:
# - StructuredOutputCapability.STRICT          # json_schema with strict:true
# - StructuredOutputCapability.BEST_EFFORT     # json_object mode
# - StructuredOutputCapability.FUNCTION_CALLING # Tool calling pattern
# - StructuredOutputCapability.NONE            # No API support
```

### How It Works

1. **Detection**: `BaseLLMProvider.get_structured_output_capability()` checks the centralized capability registry
2. **Strategy**: `get_structured_output_strategy()` creates a strategy with appropriate mode and configuration
3. **Execution**: `milestone_engine` uses the strategy to:
   - Conditionally include schema in prompt (for BEST_EFFORT/NONE modes)
   - Use correct response_format (json_schema vs json_object)
   - Apply provider-specific configurations

### Benefits

- **No Hardcoded Logic**: Adding new providers or models only requires updating the capability registry
- **Graceful Degradation**: Automatically falls back to best available mode
- **Consistent Behavior**: Same code path for all providers
- **Easy Testing**: Mock capability levels to test different scenarios

### Example: Multi-Provider Code

```python
# This code works identically for OpenAI, Groq, Anthropic, local models:
schema = InquiryResponse.model_json_schema()
strategy = provider.get_structured_output_strategy(schema)

# Conditionally include schema in prompt based on capability
if strategy.include_schema_in_prompt:
    prompt = f"{prompt}\n\nSchema: {json.dumps(schema)}"

# Use strategy-determined response_format
response = await provider.generate(
    prompt=prompt,
    response_format=strategy.response_format
)
```

## Rule of Thumb: json_object vs json_schema

| Feature | json_object (OLD) | json_schema (NEW) |
|---------|------------------|-------------------|
| **Constraint Level** | Best Effort | Guaranteed |
| **Prompt Dependency** | High ("Must use JSON") | Zero |
| **Hallucination Risk** | High (Key names change) | Zero (Keys are fixed) |
| **Stability** | Flaky on small models | Robust on Llama-3/GPT-4 |
| **Schema Parameter** | ❌ NOT supported | ✅ Required |

**FaultMaven Standard:** Always use `json_schema` with `strict: True` for structured outputs.

---

## Implementation

### Using the Utility (Recommended)

```python
from faultmaven.utils.schema_converter import create_response_format_json_schema

# Create response format from Pydantic model
response_format = create_response_format_json_schema(InquiryResponse)

# Use with LLM provider
response = await llm_provider.generate(
    prompt=prompt,
    max_tokens=4000,
    temperature=0.2,
    response_format=response_format
)

# Parse response
result = InquiryResponse.model_validate_json(response.content)
```

### Manual Format (Not Recommended)

```python
response_format = {
    "type": "json_schema",
    "json_schema": {
        "name": "InquiryResponse",
        "strict": True,
        "schema": InquiryResponse.model_json_schema()
    }
}
```

---

## Critical Requirements for strict: True

### 1. Required Fields Handling

**Problem:** For OpenAI/Groq to accept `strict: True`, the schema must handle optional fields correctly.

**Solution:** Pydantic v2's `model_json_schema()` automatically generates correct schemas:
- Optional fields use `anyOf: [type, null]` instead of `default` keyword
- Only truly required fields appear in `required` array
- No action needed if using Pydantic v2 BaseModel

**Example:**
```python
class MyResponse(BaseModel):
    required_field: str              # In required array
    optional_field: Optional[str] = None  # NOT in required array, uses anyOf
```

Generated schema:
```json
{
  "properties": {
    "required_field": {"type": "string"},
    "optional_field": {
      "anyOf": [{"type": "string"}, {"type": "null"}],
      "default": null
    }
  },
  "required": ["required_field"]
}
```

### 2. Forbidden Keywords in Strict Mode

**Problem:** Certain JSON Schema keywords are not supported with `strict: True`:
- ❌ `default` at property level (Pydantic handles this correctly)
- ❌ `format: "date-time"` (use string type)
- ❌ `enum` (use Literal type in Pydantic instead)

**Solution:** Use Pydantic's type system:
```python
# ✅ GOOD - Use Literal for enums
from typing import Literal

class Response(BaseModel):
    status: Literal["pending", "completed", "failed"]
    timestamp: str  # Not format: "date-time"

# ❌ BAD - Using Field with incompatible constraints
class Response(BaseModel):
    timestamp: str = Field(format="date-time")  # Fails in strict mode
```

### 3. Token Limit Handling (Incomplete JSON)

**Problem:** If the LLM hits token limit (`finish_reason: "length"`), the JSON will be incomplete and invalid, even with `json_schema` mode.

**Current Protection:**
```python
# milestone_engine.py line 402
return schema_model.model_validate_json(content)  # Raises ValidationError on incomplete JSON
```

The `llm_error_handler.with_retry()` catches `ValidationError` and retries automatically.

**Additional Monitoring:** Check for specific error pattern:
```python
try:
    result = schema_model.model_validate_json(content)
except ValidationError as e:
    if "expected value" in str(e):
        logger.warning(f"Possible incomplete JSON due to token limit: {e}")
    raise
```

**Prevention:**
- Set appropriate `max_tokens` (currently 4000)
- Monitor token usage in responses
- Use smaller schemas for token-constrained models

### 4. Provider Compatibility

**OpenAI (Full Support):**

- ✅ GPT-4o, GPT-4o-mini, GPT-4o-2024-08-06+ (full json_schema support)
- ✅ All models support `strict: True` with guaranteed schema adherence

**Groq (Limited Support):**

- ✅ **Strict mode supported:** `openai/gpt-oss-20b`, `openai/gpt-oss-120b` only
- ⚠️ **Fallback to json_object:** `meta-llama/Llama-3.3-70b-versatile`, `mixtral-8x7b`, and all other Groq models
- 🔄 **Auto-detection:** FaultMaven automatically falls back to json_object mode for unsupported models
- 📝 **Requires prompt:** json_object mode needs "JSON" instruction (automatically added by FaultMaven)

**Anthropic Claude:**

- ✅ Via function calling translation (handled by provider)
- ✅ Full schema enforcement through tool use

**Local Models:**

- ⚠️ Varies by model architecture
- Most Ollama models: json_object mode only
- Some fine-tuned models may support structured output

**Capability Detection:**
```python
# FaultMaven automatically detects model capabilities
# For Groq, this check happens in groq_provider.py:
if model not in STRICT_JSON_SCHEMA_MODELS:
    logger.warning(f"Falling back to json_object for {model}")
    response_format = {"type": "json_object"}  # Automatic fallback
```

**Check Model Support Manually:**
```python
# Test with simple schema first
test_schema = create_response_format_json_schema(SimpleModel)
try:
    response = await provider.generate(
        prompt="Return a test response",
        response_format=test_schema
    )
except Exception as e:
    logger.error(f"Provider doesn't support json_schema: {e}")
```

**⚠️ Important Note:**

If using **Groq with Llama-3.3-70b-versatile**, you will get json_object mode (not strict mode). This means:

- Field names may vary (e.g., `likelihood` → `confidence`)
- Validation errors possible if LLM invents new fields
- Consider switching to `openai/gpt-oss-20b` for strict mode on Groq

**Source:** [Groq Structured Outputs Docs](https://console.groq.com/docs/structured-outputs) (2025-02-01)

---

## Common Issues and Solutions

### Issue: "Unsupported property 'default'"

**Cause:** Schema contains `default` keyword at property level

**Solution:** Upgrade to Pydantic v2, which uses `anyOf` pattern

### Issue: LLM returns plain text instead of JSON

**Cause:** Invalid `response_format` parameter (e.g., mixing `json_object` with `schema`)

**Solution:** Use `create_response_format_json_schema()` utility

### Issue: ValidationError: "Invalid JSON: expected value at line 1 column 1"

**Cause:** LLM returned non-JSON content (often due to parameter mismatch)

**Solution:**
1. Verify correct `json_schema` format (not `json_object`)
2. Check provider supports JSON mode
3. Review full error log with `exc_info=True`

### Issue: Field names don't match schema

**Cause:** Using `json_object` mode instead of `json_schema`

**Solution:** Switch to `json_schema` with `strict: True` for guaranteed field names

---

## Testing Structured Outputs

### Unit Test Example

```python
from faultmaven.utils.schema_converter import create_response_format_json_schema

def test_response_format_structure():
    """Test that response format has correct structure"""
    response_format = create_response_format_json_schema(InquiryResponse)

    assert response_format["type"] == "json_schema"
    assert "json_schema" in response_format
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["name"] == "InquiryResponse"
    assert "schema" in response_format["json_schema"]

def test_schema_has_required_fields():
    """Ensure critical fields are marked required"""
    schema = InquiryResponse.model_json_schema()

    assert "agent_response" in schema["required"]
    assert "state_updates" in schema["required"]
```

### Integration Test Example

```python
async def test_llm_structured_output():
    """Test actual LLM response with json_schema"""
    response_format = create_response_format_json_schema(InquiryResponse)

    response = await llm_provider.generate(
        prompt="User reports: The login page is down",
        response_format=response_format
    )

    # Should parse without errors
    result = InquiryResponse.model_validate_json(response.content)

    # Verify structure
    assert result.agent_response
    assert result.state_updates
```

---

## Migration from json_object

If migrating legacy code:

```python
# ❌ OLD - Invalid format
response_format = {
    "type": "json_object",
    "schema": schema  # NOT SUPPORTED
}

# ✅ NEW - Correct format
response_format = create_response_format_json_schema(SchemaModel)
```

**Benefits of migration:**
- Zero field name hallucinations
- No prompt engineering needed
- Automatic retry on validation errors
- Better error messages

---

## References

- [OpenAI Structured Outputs](https://platform.openai.com/docs/guides/structured-outputs)
- [Groq JSON Mode](https://console.groq.com/docs/model/llama-3.3-70b-versatile)
- [Pydantic JSON Schema](https://docs.pydantic.dev/latest/concepts/json_schema/)
- Internal: `faultmaven/utils/schema_converter.py`
- Internal: `faultmaven/core/investigation/schemas.py`
