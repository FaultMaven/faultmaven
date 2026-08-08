# LLM Model Capabilities Reference

Provider capability matrix for FaultMaven's LLM routing system. These capabilities affect investigation quality — particularly tool calling, which enables the DA (Detective Agent) tool loop.

## Provider Capability Matrix

| Provider | Tool Calling | Structured Output | Notes |
|----------|-------------|-------------------|-------|
| OpenAI | Yes | STRICT | Full capability |
| Anthropic | Yes | FUNCTION_CALLING | Uses native tool use API |
| Groq | Yes | STRICT (some models) | Model-dependent strict support |
| Fireworks | Yes, except a denylist | BEST_EFFORT | Only `minimax-m2p7` is denylisted |
| Gemini | Yes | BEST_EFFORT | Full capability |
| Cohere | Yes | BEST_EFFORT | Full capability |
| HuggingFace | No | BEST_EFFORT | No DA tool use, degraded investigation |
| Local (Ollama/vLLM) | Model-dependent | Model-dependent | functionary/hermes: tool calling supported |
| OpenRouter | Inherited | Inherited | Depends on underlying model |

## Capability Definitions

### Tool Calling

Controls whether the provider can execute the DA tool loop (`_tool_augmented_generate()` in `milestone_engine.py`). When tool calling is unavailable, the investigation falls back to single-shot generation without evidence-gathering tools (`answer_from_kb`, `case_evidence_search`, `search_file`, etc.).

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

> **Known gap — Fireworks is classified below its actual capability.**
> `FireworksProvider.get_structured_output_capability()` returns `BEST_EFFORT`
> unconditionally, on the stated premise that "Fireworks doesn't currently
> support strict json_schema enforcement". That premise no longer holds for at
> least some models. Measured 2026-08-08 against
> `accounts/fireworks/models/deepseek-v4-flash-0731`:
>
> | Request | Result |
> |---|---|
> | `response_format: {"type": "json_schema", strict: true}` | 200 — returned **exactly** the declared keys, no extras |
> | `response_format: {"type": "json_object"}` | 200 — added an **extra** key outside the schema |
>
> That is precisely the STRICT-vs-BEST_EFFORT distinction, so the engine is
> currently leaving schema enforcement unused on Fireworks and accepting
> prompt-level drift instead. Promoting the classification is a **per-model**
> decision (a strict-capable allowlist mirroring `_TOOL_CALLING_DENYLIST`), not
> a blanket flip — support has to be verified per model before claiming it, and
> the change alters strategy selection for every Fireworks route. Until then the
> matrix row above describes what the code *does*, not the ceiling of what the
> provider *can* do.

## Known Model-Specific Behaviors

### Fireworks tool-calling denylist

`FireworksProvider.supports_tool_calling()` returns `True` for every model
**except** those in `_TOOL_CALLING_DENYLIST`, which currently holds exactly one
entry: `accounts/fireworks/models/minimax-m2p7` (forced tool use times out at
the 180s Fireworks timeout — 2026-05-20 Run 7 post-mortem).

The denylist is a *pre-check* optimisation, not the safety net: it skips the
tool-augmented path for models with a known, reproducible incompatibility so
the engine doesn't pay for the first failure every turn. One-off or transient
incompatibilities are meant to fall to the Layer-2 runtime fallback
(`ToolCallingUnsupportedError`). Add a model only after observing **repeated**
failures for it, in production or reproducible eval runs.

**DeepSeek is not denylisted.** Older DeepSeek releases (V2, R1) emitted
proprietary tool-calling tokens (`<｜tool▁calls▁begin｜>`) that Fireworks'
OpenAI-compatible tools API rejected with `400 invalid_request_error`; **V3 and
later support OpenAI-compatible tool calling** and are routed through the tool
loop like any other model. This section previously claimed
`supports_tool_calling()` returned `False` for all DeepSeek models on
Fireworks — that has not been true since the V2/R1-era block was lifted.

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

# Timeout — global default; per-provider override below for slow models
LLM_REQUEST_TIMEOUT=90
LLM_PROVIDER_TIMEOUT_OVERRIDES='{"fireworks": 180}'
```

## Error Handling

LLM errors carry retryability information:
- **4xx errors** (client errors): Non-retryable, fail fast
- **5xx errors** (server errors): Retryable with backoff
- **Timeouts**: Non-retryable. Per-provider ceiling resolves via `LLM_PROVIDER_TIMEOUT_OVERRIDES.<provider>` first, falling back to `LLM_REQUEST_TIMEOUT`. See [adding-llm-providers.md](../guides/adding-llm-providers.md) for the three-layer resolution rule.

See `LLMException` in `faultmaven/exceptions.py` for the retryability system.
