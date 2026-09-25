---
paths:
  - "faultmaven/infrastructure/llm/**"
  - "faultmaven/core/investigation/**"
  - "faultmaven/modules/agent/**"
  - "faultmaven/config/settings.py"
  - "faultmaven/config/investigation_capability.py"
  - "faultmaven/utils/schema_converter.py"
  - "faultmaven/utils/token_estimation.py"
  - ".env.example"
  - "tests/unit/infrastructure/llm/**"
  - "tests/unit/core/investigation/**"
  - "tests/unit/architecture/test_llm_rules_pin_reasoning_intent_call_sites.py"
---

# LLM providers, structured output and the turn budget

Loaded when LLM-facing code is touched. Two tests pin this file:
`tests/unit/infrastructure/llm/test_groq_model_defaults.py` (the Groq row of
the provider table names the shipped default) and
`tests/unit/architecture/test_llm_rules_pin_reasoning_intent_call_sites.py`
(the `| Call site | Declares |` table and every "N call sites" count match the
code, and the quoted `TOOLLESS_INFERENCE_OUTPUT_FLOOR` value matches
`milestone_engine.py`).

## Supported LLM Providers

| Provider | Environment Variable | Models | Structured output | Notes |
|----------|---------------------|--------|-------------------|-------|
| Anthropic | `ANTHROPIC_API_KEY` | claude-sonnet-4-6 | **FUNCTION_CALLING** | Schema enforced via forced tool use; recommended for logic |
| OpenAI | `OPENAI_API_KEY` | gpt-5.6-luna | **STRICT** (gpt-4o+) | Reasons by default: plain calls send `reasoning_effort: "none"` and tool calls force it (hard model constraint), while structured calls keep the `low` starvation floor — so `OPENAI_REASONING_EFFORT=none` is a no-op here (and warns per structured call) — leave it unset. A HIGHER value is NOT inert: it replaces the `"none"` shape default on plain calls. `gpt-5.4-mini` remains supported |
| Google Gemini | `GEMINI_API_KEY` | gemini-3.7-flash | **STRICT** (1.5+) | **Shipped default provider + model.** Fast multimodal. The adapter version-gates the reduced 3.6/3.7 API surfaces (no sampling params, `thinkingLevel`-only, user-role function responses carrying the call id); `gemini-3.5-flash*` remain supported on the classic surface |
| Fireworks AI | `FIREWORKS_API_KEY` | accounts/fireworks/models/deepseek-v4-flash | BEST_EFFORT | Strong open weights, but schema not enforced — see note |
| Groq | `GROQ_API_KEY` | openai/gpt-oss-20b | **STRICT** (gpt-oss only; BEST_EFFORT otherwise) | Ultra-fast inference. Groq serves no Llama chat model any more — `llama-3.3-70b-versatile` 404s. Per-org TPM limits are low on the free tier (8k on `on_demand`), so a full-document call can 413 where a classifier-sized one succeeds |
| HuggingFace | `HUGGINGFACE_API_KEY` | Mistral-Large-Instruct-2411 | BEST_EFFORT | Open models — NOT recommended (no tool calling) |
| Cohere | `COHERE_API_KEY` | command-r-plus | BEST_EFFORT | Enterprise RAG (json_object only; not schema-enforced) |
| OpenRouter | `OPENROUTER_API_KEY` | anthropic/claude-sonnet-4-6 | depends on routed model | Multi-model gateway (STRICT for `openai/*`, else FUNCTION_CALLING) |
| Local (Ollama/vLLM) | `LOCAL_LLM_URL` | llama3.2, etc. | FUNCTION_CALLING (functionary/hermes on OpenAI-compatible transport only), else BEST_EFFORT | Private & offline. **The URL path picks the protocol** — bare host or `/v1` = OpenAI-compatible, `/api` = Ollama native — and one predicate (`resolve_local_transport`) decides both that dispatch and the capability answer, so they cannot disagree. Tool calling follows: assumed on the OpenAI-compatible path, impossible on `/api/generate` for any model. Never keyed on the hostname (#1356), so `http://ollama:11434/v1` is capable. `LOCAL_LLM_TOOL_CALLING=false` declares a stack built without tool support |

The default chat model per provider is declared three times — `config/settings.py`,
`infrastructure/llm/providers/registry.py` `default_model`, and `.env.example` — and
`scripts/check_env_example_sync.py` enforces that they agree. Published rates for these
models live in `infrastructure/llm/pricing.py`. Measured per-model behaviour is in
`docs/reference/llm-model-capabilities.md`.

## Capability roles

The shipped defaults are Gemini-anchored: `CHAT_PROVIDER=gemini` with
`GEMINI_MODEL=gemini-3.7-flash`, and `classifier`/`synthesis`/`multimodal`
pinned to gemini (the two small-output roles on `gemini-3.5-flash-lite`).
`da`/`knowledge`/`structured_output` ship unset, so they follow
`CHAT_PROVIDER` — flipping the anchor moves them and leaves the pins put,
which is what makes an A/B comparison of the anchor a controlled one.

Any role can be reassigned:

```bash
CHAT_PROVIDER=anthropic      # the anchor: unset roles follow it
CODE_PROVIDER=openai         # Code generation tasks
MULTIMODAL_PROVIDER=gemini   # Image analysis
SYNTHESIS_PROVIDER=fireworks # Fast JSON generation
CLASSIFIER_PROVIDER=groq     # Query routing
```

`GET /admin/llm/config` reports `role_routing` — the resolved (provider, model)
for every role with its provenance (#1206): which env key decided it, whether
the provider was set for that role or `inherited`, and whether it is actually
initialized (an uncredentialed pin is inert and falls back to the chain).

## Structured output

**Structured-output enforcement matters.** The investigation engine drives state
from large schema-constrained LLM responses. **STRICT** providers enforce the
schema natively (tool calling / `json_schema` response_format) — the engine gets
valid state. **BEST_EFFORT** providers only request the schema in-prompt: the
model can omit required fields, the engine drops the `state_updates`, and you get
empty/degraded investigations. **Use a STRICT provider as `CHAT_PROVIDER`**
(OpenAI, Anthropic, Gemini 1.5+). BEST_EFFORT providers (Fireworks incl.
`deepseek-v3`/minimax, Groq, HuggingFace, Local) are fine for cheap
`CLASSIFIER_PROVIDER`/`SYNTHESIS_PROVIDER` overrides but degrade primary CHAT.
Capability is reported per-provider via `get_structured_output_capability()`.

**A STRICT provider only enforces schemas that can be expressed strictly.**
OpenAI's strict subset admits no optional keys and no free-form objects, so
`utils/schema_converter.to_strict_schema` rewrites a schema to fit — every
property required, formerly-optional ones rendered as null unions,
`additionalProperties: false` throughout — and **refuses** when it cannot. All
six response schemas are enforced: `InquiryResponse`, `TerminalResponse` and the
four `InvestigationResponse_*`. The refusal valve remains for any schema that
later grows a construct outside the subset — a free-form `Dict[str, Any]` is the
one the project hit, and it is refused rather than sent with a `strict: true`
the API rejects. Enforcement is scoped to the schema tool; investigation tools
keep optional parameters.

**The rewrite keeps the schema's value constraints** (`minimum`/`maximum`,
`maxLength`, `pattern`, `minItems`/`maxItems`) — it drops only `default`,
`examples`, `format` and the handful of keywords an API refuses. It used to
drop the narrowing ones too, as "descriptive", and the Gemini adapter stripped
them a second time; the engine's `likelihood: Field(ge=0, le=1)` therefore
reached the model as a bare number and came back as `95`, which Pydantic
rejects and the turn 500s (fm#355). Routing the schema tool through Gemini's
`response_schema` instead of function calling enforces **nothing** extra,
because `generationConfig.responseSchema` and `FunctionDeclaration.parameters`
are the same `Schema` message in the API and go through the same adapter
resolver — the lever is what the schema contains, not which field carries it.
Measured enforcement per provider, and where `maxLength` stops biting, is in
`docs/reference/llm-model-capabilities.md` §"Value constraints"; the guard is
`tests/unit/core/investigation/test_schema_constraints_reach_the_provider.py`,
written end-to-end over both builders so a third stripper anywhere on either
path fails it.

The investigation schemas reached the subset by giving their two `Dict[str, Any]`
fields declared shapes (fm#1057). `milestone_justifications` became
`MilestoneJustifications`, one field per settable milestone — its key domain was
always closed, so the wire shape (an object keyed by milestone name) is
unchanged. `hypotheses_to_update` became a list whose entries carry
`hypothesis_id`, because there the key domain is genuinely open.

Because a strict response carries every key with `null` where the model had
nothing, schema classes with **defaulted** fields inherit `NullTolerantModel`,
which restores the default. The rule keys on "has a non-`None` default" rather
than "is not `Optional`": the `Optional[List[X]] = default_factory=list` case
accepts the null silently and would otherwise land as `None` in code expecting a
list — 47 such fields in the investigation schemas alone. A field whose default
*is* `None` keeps its `None`. Two consequences worth knowing: reading
`milestone_justifications` must go through `as_dict()` (a plain `model_dump()`
reports every milestone as justified and the reasoning gate stops firing), and a
new gate milestone needs a matching justification field or it becomes
unjustifiable — both pinned by `tests/unit/core/investigation/test_schema_strict_mode.py`.

## Thinking models and the starvation cap

STRICT enforcement is necessary but not sufficient: a STRICT **thinking** model
bills hidden reasoning against `maxOutputTokens`, which can starve the JSON
output on deep-context turns (truncation to `MAX_TOKENS` → 500). The Gemini
provider caps thinking on structured calls for **Gemini 3.x only** via
`thinkingConfig.thinkingLevel: "low"` (3.x dropped the 2.5-era integer
`thinkingBudget`). This is scoped to 3.x because that's where the starvation was
observed (gemini-3.5-flash, the then-default); Gemini 2.5 is left at native dynamic
thinking — it ran clean, and capping it would change a working reasoning path
without evidence.

On the **Gemini 3.7+ API surface** (`gemini-3.7-flash` onward; version-gated in
the adapter as `(major, minor) >= (3, 7)`) the cap widens to **every call
shape**, plain chat included: `thinkingLevel` is the only reasoning knob left
there, the server default is `medium`, thinking bills at the full output rate,
and the product profile for this path is little/no reasoning at low latency.
The same surface gate also strips the removed sampling params
(`temperature`/`topP`/`topK`). A second, EARLIER gate (`>= (3, 6)`) versions
the tool-result shape: 3.6 rejects the classic `role: "function"` turn
outright, so from 3.6 the adapter sends function responses as `role: "user"`
turns carrying `id` + `name` (paired with the `functionCall.id` the API
issues, which the adapter adopts as `ToolCall.id`; mandatory per the 3.7
migration guide). Requests to 3.5-generation models are byte-for-byte
unchanged — the classic shape measured working end-to-end (2026-08-26).
Details: `docs/reference/llm-model-capabilities.md` §"Gemini 3.6/3.7 API
surfaces".

That shape-based rule is the **default**, and a caller can refine it per
call with a **reasoning intent** (`#1118`, below): `EXTRACTION` extends the cap
to plain 3.x calls as well, and `INFERENCE` *lifts* it on structured calls —
but only when the same call also declares an output floor, without which the
provider refuses the lift and warns. (On the 3.7+ surface `INFERENCE` also
lifts the all-shape default on plain calls, floor or no floor — plain-call
starvation is non-fatal.) So "3.x structured calls are capped" is the
default, not an invariant: **five call sites declare an intent** (see below),
four of them `EXTRACTION` — the direction that asks for *less* reasoning,
which can never lift a cap — and one `INFERENCE` on a **structured** call,
the tool-less single-shot diagnostic turn (fm#1116), which lifts the cap
deliberately and declares `TOOLLESS_INFERENCE_OUTPUT_FLOOR` to buy the lift.
Reasoning here is **routed**, not suppressed: the provider's minimum where the
model is transforming supplied context, its default where the model is
reasoning over candidates.

## Stop reasons and truncation

**Every response carries a normalised stop reason.** `LLMResponse.stop_reason`
(`STOP | MAX_TOKENS | CONTENT_FILTER | TOOL_CALLS | UNKNOWN`, with a derived
`is_truncated`) is populated by all nine providers from whatever their API
calls it — `finish_reason: "length"`, `stop_reason: "max_tokens"`,
`finishReason: "MAX_TOKENS"`, `done_reason`, llama.cpp's `stopped_limit`. Map
new providers with `normalize_stop_reason()`; never match the raw strings, and
never write a placeholder into `content` (that channel was retired in #1094).
`UNKNOWN` means "no signal", not "finished" — HuggingFace as called supplies
none — so it must never be collapsed into False, or every `if is_truncated`
fails open. Consumers recover via
`infrastructure/llm/truncation.generate_with_truncation_retry` (double the cap
once); what happens if the retry is also cut is per-consumer — read paths
(KB/evidence QA, tier-2) annotate and return the partial, write paths (runbook
conversion) refuse to persist. The one exception is a call carrying an output
floor (below): there a starved first attempt arrives as an exception rather
than a response, the helper spends its one retry on it exactly as it would on
a cut body, and a twice-starved call raises instead of returning a partial —
the caller pre-declared that partial unusable. Pass `min_output_tokens` to the
helper as well as to `route()`, or its doubling is computed from a cap the
router has already raised and the "retry" repeats the first attempt
verbatim. The reason is logged and counted at the router
(`llm_stop_reasons_total`), and a response reported as cut is never written to
the response cache — the key omits `max_tokens`, so a stored cut body is what a
retry at a bigger cap would be served instead of reaching the provider.

## The retry ladder is budgeted against the turn deadline

A turn is bounded by `AGENT_REQUEST_TIMEOUT` (per-provider via
`AGENT_PROVIDER_TIMEOUT_OVERRIDES`), applied as an `asyncio.wait_for` at the
turn route. The ladder inside it (`LLMErrorHandler.with_retry`, `max_retries=3`)
costs `3T + 14s` against a hung provider, where `T` is the resolved per-call
ceiling — `max(LLM_REQUEST_TIMEOUT, LLM_PROVIDER_TIMEOUT_OVERRIDES[provider])`,
NOT `LLM_REQUEST_TIMEOUT` alone, which understates the shipped cluster shape
threefold. Three PAID attempts, because the router's breaker opens on the third
failure and short-circuits the fourth, plus all four backoffs (2 + 4 + 8; the
eight seconds are spent before the attempt the breaker refuses). The two settings
live in different classes with their own per-provider maps and nothing related
them, so the turn used to be cancelled mid-ladder: the classification never ran
and the caller got an opaque 504 instead of the honest 503 + `Retry-After`
(#1278/#1292). `core/investigation/turn_budget.py` binds the deadline in a
`ContextVar` at the one site that applies the `wait_for`.
`LLMRouter._resolve_timeout` then **clamps every router-borne call's own
timeout** to what is left — which bounds intent classification and KB/document
synthesis too, not just the ladder, and keeps a cut-short call on the path that
records a circuit-breaker failure (an outer `wait_for` would cancel it, and
`CancelledError` is invisible to `call_external`'s handlers). `with_retry` adds
a later backstop clamp for providers reached without the router, and **refuses a
retry** whose backoff plus another attempt of the worst cost observed *in that
ladder* would not fit. Refusing the ladder's last iteration reports
`RETRY_EXHAUSTED` (nothing that could have changed the answer was lost);
refusing an earlier one reports `TURN_BUDGET_EXHAUSTED`, whose remediation is
the config. Both → 503. **Unbound context means unbounded**: outside a
turn the budget reads `None` and every check is inert, which is why the binding
is the seam the whole guard hangs on and has its own test. Whether a
deployment's two timeouts actually fit is reported by
`GET /admin/config/status` as `llm_retry_ladder_fits_turn_budget`.

## Tool calling is required for the investigation role

Directed Analysis (`search_file`, `deep_analysis`) needs function/tool calling;
a tool-incapable model can't gather evidence yet would still conclude — the
premature-conclusion failure FaultMaven guards against. A **startup fail-fast
gate** (`config/investigation_capability.py`, `validate_investigation_tooling`,
called from the lifespan beside the deployment-coherence and credential gates)
**refuses to boot** when the resolved investigation model (`DA_PROVIDER` →
`CHAT_PROVIDER`) can't do tool calling — unless
`ALLOW_TOOLLESS_INVESTIGATION=true` (knowing opt-in to degraded/offline mode;
`/health` then reports `degraded`). The per-turn runtime fallback in
`milestone_engine` still covers transient tool failures on an otherwise-capable
model. Capability is per-provider/model via `supports_tool_calling()`
(HuggingFace: always False; Fireworks: a denylist for models that accept tools
but time out on forced `tool_choice=required`; Local: derived from the
transport the URL path names, and overridable by the operator — a self-hosted
endpoint has no catalogue to key a denylist on, and its capability is a
property of the serving stack rather than of the model's or the host's name).

## Reasoning intent and the output floor

**A caller can declare what a call needs from reasoning, and the minimum
output it can use.** Two optional, per-call-site knobs on `LLMRouter.route()`
(#1118 / #1117) — and, since `milestone_engine` binds a concrete provider
rather than routing, on the provider `generate()` signatures underneath it.
Both default to absent, and where a call site passes neither the shape-based
provider defaults above are what runs. **Five call sites ship declaring
them**, four `EXTRACTION` and one `INFERENCE`:

| Call site | Declares |
|-----------|----------|
| `core/investigation/intent_resolver.py` (intent classifier) | `reasoning_intent=EXTRACTION`, `min_output_tokens=CLASSIFIER_MIN_OUTPUT_TOKENS` |
| `modules/agent/tools/document_qa_tool.py` (KB/doc answer synthesis) | `reasoning_intent=EXTRACTION` |
| `modules/agent/domain/services/out_of_band.py` (out-of-band triage, #1329) | `reasoning_intent=EXTRACTION`, `min_output_tokens=TRIAGE_MIN_OUTPUT_TOKENS` |
| `modules/agent/domain/services/out_of_band.py` (out-of-band answer, #1329) | `reasoning_intent=EXTRACTION` |
| `core/investigation/milestone_engine.py` (tool-less single-shot diagnostic turn, fm#1116) | `reasoning_intent=INFERENCE`, `min_output_tokens=TOOLLESS_INFERENCE_OUTPUT_FLOOR` |

Paths are relative to `faultmaven/`. The four `EXTRACTION` sites are grounded
transformations of supplied context rather than reasoning over candidates, so
they ask for *less* and can never lift a starvation guard. `document_qa_tool`
declares the intent specifically so its cap is tier-independent: the shape
default caps thinking only on the 3.7+ surface, so on the shipped
`gemini-3.5-flash-lite` synthesis pin that plain call would otherwise run
uncapped.

The `INFERENCE` site is the one call that asks for *more*, and it is a
**structured** call. fm#1116: a turn with nothing to search takes a single-shot
structured path instead of the tool loop, because gpt-5.x must pin
`reasoning_effort: "none"` whenever function tools are attached — so a turn
that runs the loop reasons at zero even though it has no tool to use, and
diagnosis is reasoning over candidate causes, not extraction.
`TOOLLESS_INFERENCE_OUTPUT_FLOOR` (2048) is what buys the lift: the Gemini
provider refuses to lift a structured call's cap unless a floor is declared,
and 2048 sits above every completion body measured on the replayed turn while
staying well under `STRUCTURED_OUTPUT_MAX_TOKENS`, so it forbids a starvable
partition without ever raising the cap. Note where the floor is and is not
enforced: this path reaches the provider **directly**, so the router's own
enforcement (pre-call budget bump, post-call `LLMOutputFloorError`) does not
run on it — a body cut anyway is caught by the structured-output truncation
ladder instead. On this path the floor's job is to authorise the lift, not to
police the result.

The knobs themselves:

- `reasoning_intent` — `EXTRACTION` (grounded transformation of supplied
  context: ask for the provider's minimum reasoning) or `INFERENCE` (reasoning
  over candidates: ask for its default). Semantic on purpose — `reasoning_effort`
  is OpenAI's word, `thinkingLevel` is Gemini's, and a call site has no business
  knowing which provider answered. Each provider translates it; **hard model
  constraints override it** (gpt-5.6 keeps its forced `reasoning_effort: "none"`
  alongside tools); an intent that cannot be applied is **logged, never silently
  dropped** — WARNING for `INFERENCE`, INFO for `EXTRACTION`. (An intent that
  *was* delivered is not reported as a failure: on gpt-5.6 with tools the
  forced `"none"` is exactly what `EXTRACTION` asked for.)
- `min_output_tokens` — the minimum *visible* output the call needs. Reasoning
  bills against the same budget as the answer on every provider (no second pool
  exists), so this is a floor, not a new budget. Pre-call it raises `max_tokens`
  to at least the floor; post-call a `MAX_TOKENS` stop measuring below it raises
  `LLMOutputFloorError` rather than returning a body the caller pre-declared
  unusable. Visible output is measured with the provider's real tokenizer when
  the provider has one (`utils/token_estimation.estimate_tokens` covers openai,
  openrouter, anthropic and fireworks) **and** the body is at or under
  `_TOKENIZER_EXACT_MAX_CHARS` (4000 — tiktoken is super-linear on the degenerate
  looped-output shape, 1.3 s of blocking CPU at 32k chars). Everything else —
  the five providers it cannot tokenize, and any longer body — is **deliberately
  bounded above by its UTF-8 byte count**. The governing invariant is that the
  estimate must never UNDER-state: under-stating fires the guard on a body that
  met its floor and kills an otherwise-usable turn, while over-stating only wastes
  a guard. Bytes are what make that *provable* rather than merely measured — a
  byte-level BPE token consumes at least one byte, so N bytes cannot exceed N
  tokens for any script. (Counting *characters* is only the ASCII corollary and
  breaks outside it: `'🔥🚀💡'*100` is 300 characters but 800 tokens.) That is also
  why neither a raw `len//4` (shape-dependent, understates dense JSON/CJK by 2-4x)
  nor a scaled prefix sample (sound only under uniform density) is used. The cost
  is guard reach on long bodies, which is affordable because starvation produces
  short ones — and short bodies are tokenized exactly.

Two invariants worth knowing before adopting them: **`INFERENCE` requires
`min_output_tokens`** (it lifts starvation guards, and the floor is what makes
that safe — the router raises `ValueError`, and the Gemini provider independently
refuses the lift, because `milestone_engine` reaches providers without going
through the router); and the **floor is unenforceable on a provider reporting no
stop reason** (`UNKNOWN`) — a starved-looking body is warned about and returned,
since "cut" and "short" are indistinguishable there. The pre-call bump raises
`max_tokens` only *to* the floor, leaving no room for hidden reasoning: on a
reasoning path the caller sizes `max_tokens` above the floor itself.

## Adding a New LLM Provider

1. Implement the provider in `infrastructure/llm/providers/`, inheriting from `BaseLLMProvider` in `base.py`.
2. Merge leftover kwargs into the request body with **`self._merge_extra_kwargs(payload, kwargs, model=...)`**, never a hand-rolled `payload.update(kwargs)` — routing-level knobs (`reasoning_intent`, `min_output_tokens`, `cache_prompt`) are not API fields, and a provider that merges them raw sends them as body fields on every call and is rejected by any OpenAI-compatible endpoint. Consume a knob (pop it) only if the provider actually translates it.
3. Populate `stop_reason` via `normalize_stop_reason()` (see "Stop reasons and truncation") — `UNKNOWN` is the honest default, not `STOP`.
4. Register in `infrastructure/llm/providers/registry.py`.
5. Add config in `config/settings.py` and document it in `.env.example` (`scripts/check_env_example_sync.py` checks the three default-model declarations agree).
6. Add tests in `tests/unit/infrastructure/` — `tests/unit/infrastructure/llm/providers/test_reasoning_intent.py` derives its coverage from `PROVIDER_SCHEMA`, so a newly registered provider is checked for knob handling automatically.
7. Add a row to the provider table above (and a pricing entry in `infrastructure/llm/pricing.py` if the model is billed).

Long-form guide: `docs/guides/adding-llm-providers.md`. Capability detection
design: `docs/architecture/core-architecture/structured-output-capability-system.md`.

## Diagnosing a provider locally

`docs/development/local-troubleshooting.md` §LLM provider issues — the resolved
role routing is read from `GET /api/v1/admin/llm/config`, never by echoing keys.
