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

## Role Routing

Every LLM-calling role can be pinned to its own (provider, model). The role
provider falls back to `CHAT_PROVIDER` when unset; the role's model resolves
`{PROVIDER}_{ROLE}_MODEL` → that provider's `{PROVIDER}_MODEL` — so two roles
on the SAME provider can run different models (e.g. `OPENAI_MODEL` for chat,
a cheaper `OPENAI_CLASSIFIER_MODEL` for the classifier).

| Role env key | Routes which function | Falls back to | Notes |
|---|---|---|---|
| `CHAT_PROVIDER` | Everything by default: investigation engine turns, all conversation | — | Required; boot refuses without it and its API key |
| `DA_PROVIDER` | Directed Analysis tool loop — the evidence-gathering iterations (`search_file`, `deep_analysis`, KB lookups) inside investigation turns | `CHAT_PROVIDER` | The startup tool-calling gate validates the resolved DA→CHAT (provider, model) |
| `STRUCTURED_OUTPUT_PROVIDER` | Schema-bound engine calls (the response-schema tool / structured output) | `CHAT_PROVIDER` | The escape hatch for a best-effort chat provider: force just the schema-bound calls onto a STRICT-capable provider. A DA override gets first dibs on the tool path |
| `CLASSIFIER_PROVIDER` | Intent resolver (typed replies vs. offered suggestions) + document triage in knowledge preprocessing | `CHAT_PROVIDER` | Best-effort models are acceptable here (small, enum-like outputs) |
| `SYNTHESIS_PROVIDER` | QA sub-agent answer synthesis — `kb_qa` / `global_kb_qa` / `user_kb_qa` / `case_evidence_qa` / `document_qa` answers | `CHAT_PROVIDER` | Best-effort acceptable here too |
| `KNOWLEDGE_PROVIDER` | Document→runbook conversion (failure-mode analysis + runbook drafting) | `CHAT_PROVIDER` | |
| `MULTIMODAL_PROVIDER` | Visual extractor (image/screenshot analysis in preprocessing) | `CHAT_PROVIDER` | Builds its own client from provider+key+model; does not go through the router |
| `CODE_PROVIDER` | **Nothing — unwired.** The setting, getter and per-task model fields exist, but no code path consumes them | — | Wire it or remove it; until then it is dead config surface |

Two per-role **model** keys need a caveat the table above does not carry:

- `{PROVIDER}_DA_MODEL` **requires `DA_PROVIDER`.** The investigation tool loop
  passes a per-role model only when a dedicated DA provider exists
  (`milestone_engine._tool_augmented_generate` sets `model` under
  `if self.da_model and self.da_provider`), so setting only `OPENAI_DA_MODEL`
  leaves the base `OPENAI_MODEL` running, silently. Every other role passes its
  model unconditionally. Set `DA_PROVIDER` alongside it — it may name the same
  provider as `CHAT_PROVIDER`.
- `{PROVIDER}_CHAT_MODEL` is **dead config surface**, alongside
  `CODE_PROVIDER`. The field and `get_model("chat")` exist, but the registry
  builds each provider with `default_model = {PROVIDER}_MODEL` and no call site
  requests the per-task chat model, so a value here never reaches a provider.
  The boot enforcement-class check judges the chat role by the base
  `{PROVIDER}_MODEL` for exactly this reason.

Role routing is **static assignment**, not fallback: an explicitly-set role
provider is routed deterministically (no fallback chain for that call) and
needs its own `*_API_KEY` configured. Startup checks evaluate the resolved
per-role models: tool calling (hard gate, DA→CHAT), engine-schema capacity
(hard gate, STRUCTURED_OUTPUT→CHAT), and schema-enforcement class (warning
when investigation/chat resolve to a BEST_EFFORT model; classifier/synthesis
exempt by design).

Provider-wide reasoning knobs (not per-role): `OPENAI_REASONING_EFFORT`
(none|low|medium|high, unset = shape-based defaults) and
`ANTHROPIC_THINKING_MODE` / `ANTHROPIC_THINKING_BUDGET_TOKENS`. The
key-by-key reference with defaults is `.env.example` (CI-synced to
`settings.py`).

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

Provider and model selection is configured in `config/settings.py` under `LLMSettings`. The fallback chain determines which provider is primary and which are fallbacks (with `STRICT_PROVIDER_MODE=true`, the default, there are no fallbacks).

```env
# Provider selection — always set the model EXPLICITLY with the provider
CHAT_PROVIDER=fireworks
FIREWORKS_MODEL=accounts/fireworks/models/deepseek-v4-flash

# Per-role overrides (see Role Routing above)
# CLASSIFIER_PROVIDER=gemini
# GEMINI_CLASSIFIER_MODEL=gemini-3.5-flash-lite

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
