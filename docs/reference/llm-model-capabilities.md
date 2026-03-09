# LLM Model Capabilities Reference

Provider capability matrix for FaultMaven's LLM routing system. These capabilities affect investigation quality — particularly tool calling, which enables the DA (Detective Agent) tool loop.

## Provider Capability Matrix

| Provider | Tool Calling | Structured Output | Notes |
|----------|-------------|-------------------|-------|
| OpenAI | Yes | STRICT | Full capability |
| Anthropic | Yes | FUNCTION_CALLING | Uses native tool use API |
| Groq | Yes | STRICT (some models) | Model-dependent strict support |
| Fireworks | Model-dependent | BEST_EFFORT | DeepSeek models: no tool calling |
| Gemini | Yes | BEST_EFFORT | Full capability |
| Cohere | Yes | BEST_EFFORT | Full capability |
| HuggingFace | No | BEST_EFFORT | No DA tool use, degraded investigation |
| Local (Ollama/vLLM) | Model-dependent | Model-dependent | functionary/hermes: tool calling supported |
| OpenRouter | Inherited | Inherited | Depends on underlying model |

## Capability Definitions

### Tool Calling

Controls whether the provider can execute the DA tool loop (`_tool_augmented_generate()` in `milestone_engine.py`). When tool calling is unavailable, the investigation falls back to single-shot generation without evidence gathering tools (`global_kb_qa`, `case_evidence_qa`, etc.).

**Impact of no tool calling:**
- No dynamic evidence retrieval during investigation
- LLM must work with only the pre-assembled context
- Lower investigation quality for complex cases

**Detection:** `provider.supports_tool_calling(model)` — checked before entering the tool loop. Runtime failures also caught via `ToolCallingUnsupportedError`.

### Structured Output Capability

Defines how reliably the provider produces valid JSON matching a schema.

| Level | Mode | Description |
|-------|------|-------------|
| `STRICT` | `json_schema_strict` | Guarantees exact schema adherence (field names, types, no extras) |
| `BEST_EFFORT` | `json_object` | Attempts schema adherence via prompt; may rename fields or omit values |
| `FUNCTION_CALLING` | `function_calling` | Uses tools API for structured output |
| `NONE` | `prompt_only` | No structured output support; JSON parsed from natural language |

**Detection:** `provider.get_structured_output_capability(model)` and `provider.get_structured_output_strategy(schema, model)`.

## Known Model-Specific Behaviors

### DeepSeek on Fireworks
- Uses proprietary tool-calling tokens (`<｜tool▁calls▁begin｜>`, `<｜tool▁calls▁end｜>`) incompatible with Fireworks' OpenAI-compatible tools API
- Produces `400 invalid_request_error` when tools are sent
- `supports_tool_calling()` returns `False` for all DeepSeek models on Fireworks

### HuggingFace Inference API
- Does not support OpenAI-compatible tool calling
- `supports_tool_calling()` always returns `False`

### Local Models (Ollama/vLLM)
- Tool calling support depends on the specific model
- `functionary` and `hermes` model families support tool calling
- Other models default to no tool calling support

## Configuration

Provider and model selection is configured in `config/settings.py` under `LLMSettings`. The fallback chain determines which provider is primary and which are fallbacks.

```env
# Provider selection
LLM_PROVIDER=fireworks
FIREWORKS_MODELS=accounts/fireworks/models/deepseek-v3

# Timeout
LLM_REQUEST_TIMEOUT=90
```

## Error Handling

LLM errors carry retryability information:
- **4xx errors** (client errors): Non-retryable, fail fast
- **5xx errors** (server errors): Retryable with backoff
- **Timeouts**: Non-retryable (configurable via `LLM_REQUEST_TIMEOUT`)

See `LLMException` in `faultmaven/exceptions.py` for the retryability system.
