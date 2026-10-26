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
`milestone_engine/generation.py`).

## Supported LLM Providers

| Provider | Environment Variable | Models | Structured output | Notes |
|----------|---------------------|--------|-------------------|-------|
| Anthropic | `ANTHROPIC_API_KEY` | claude-sonnet-4-6 | **FUNCTION_CALLING** | Schema tool forced where the model accepts forcing, otherwise `auto` plus an instruction naming it (per-model rule: `docs/reference/llm-model-capabilities.md` §"Anthropic request shape"); recommended for logic |
| OpenAI | `OPENAI_API_KEY` | gpt-5.6-luna | **STRICT** (gpt-4o+) | Reasons by default: plain calls send `reasoning_effort: "none"` and tool calls force it (hard model constraint), while structured calls keep the `low` starvation floor — so `OPENAI_REASONING_EFFORT=none` is a no-op here (and warns per structured call) — leave it unset. A HIGHER value is NOT inert: it replaces the `"none"` shape default on plain calls. `gpt-5.4-mini` remains supported |
| Google Gemini | `GEMINI_API_KEY` | gemini-3.7-flash | **STRICT** (1.5+) | **Shipped default provider + model.** Fast multimodal. The adapter version-gates the reduced 3.6/3.7 API surfaces (no sampling params, `thinkingLevel`-only, user-role function responses carrying the call id); `gemini-3.5-flash*` remain supported on the classic surface |
| Fireworks AI | `FIREWORKS_API_KEY` | accounts/fireworks/models/deepseek-v4-flash | BEST_EFFORT | Strong open weights, but schema not enforced — see note |
| Groq | `GROQ_API_KEY` | openai/gpt-oss-20b | **STRICT** (gpt-oss only; BEST_EFFORT otherwise) | Ultra-fast inference. Groq serves no Llama chat model any more — `llama-3.3-70b-versatile` 404s. Per-org TPM limits are low on the free tier (8k on `on_demand`), so a full-document call can 413 where a classifier-sized one succeeds |
| HuggingFace | `HUGGINGFACE_API_KEY` | Mistral-Large-Instruct-2411 | BEST_EFFORT | Open models — NOT recommended (no tool calling) |
| Cohere | `COHERE_API_KEY` | command-r-plus | BEST_EFFORT | Enterprise RAG (json_object only; not schema-enforced) |
| OpenRouter | `OPENROUTER_API_KEY` | anthropic/claude-sonnet-4-6 | depends on routed model | Multi-model gateway (STRICT for `openai/*`, else FUNCTION_CALLING) |
| Local (Ollama/vLLM) | `LOCAL_LLM_URL` | llama3.2, etc. | FUNCTION_CALLING (functionary/hermes on OpenAI-compatible transport only), else BEST_EFFORT | Private & offline. **The URL path picks the protocol** — bare host or `/v1` = OpenAI-compatible, `/api` = Ollama native — and one predicate (`resolve_local_transport`) decides both that dispatch and the capability answer, so they cannot disagree. Tool calling follows: assumed on the OpenAI-compatible path, unavailable on the `/api` (`/api/chat`) transport for any model. A call carrying `messages` is sent as a conversation on every local transport or refused before any request: Ollama native posts `/api/chat`; the raw llama.cpp `/completion` fallback takes one prompt, so it refuses a conversation with `CallShapeUnsupported` (non-retryable, naming `/v1/chat/completions`); the registry records no provider failure for it and tries the next provider (#2016). Never keyed on the hostname (#1356), so `http://ollama:11434/v1` is capable. `LOCAL_LLM_TOOL_CALLING=false` declares a stack built without tool support |

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
empty/degraded investigations. **Use an enforcing provider as `CHAT_PROVIDER`**:
STRICT (OpenAI gpt-4o+, Gemini 1.5+) or Anthropic, which is FUNCTION_CALLING
(the schema tool, no `strict`) and enforced only where the model accepts forced
tool use. Above the forcing ceiling the tool is requested by instruction, not
enforced, and startup warns (`forced_tool_choice_enforced()`) unless an
enforcing `STRUCTURED_OUTPUT_PROVIDER` carries the role's schema-bound calls,
which it logs at INFO. The engine does
not rely on forcing: a forced call's reply with no tool call, neither cut nor
refused, gets one re-ask per structured call on the single-shot
FUNCTION_CALLING path (#662's minimal-prompt degrade is a call of its own;
other single-shot strategies are unchanged), or is nudged with every tool on a DA
iteration whose next iteration is not the final one, on every provider (#1755).
BEST_EFFORT providers (Fireworks incl.
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

The **Anthropic** adapter gates its request shape on the model the same way
(#1695, measured live 2026-09-28). Newer Claude models 400 on any
`temperature` (from `claude-opus-4-7`), and the newest also 400 on forced
tool use (`claude-opus-5-5` and `claude-fable-5-1`; `claude-mythos-5-1` per
the model docs). So `temperature` is sent only up to a per-family ceiling.
Above the forcing ceiling, `tool_choice="required"` is sent as `auto`, plus
a trailing `system` block that names the tool when the forcing names one or
exactly one tool is offered. There the thinking-under-forcing refusal
does not apply. An unparseable id or a version above its ceiling takes
that newest shape, which every measured model accepts. The thinking shape
is the exception: no shape is accepted by every model, so an unparseable id
is sent the configured `ANTHROPIC_THINKING_MODE`, and a parsed id above its
ceiling is sent `adaptive` (#1756). Omitting `thinking` is not "off" on
every model (opus-5-5 and fable-5-1 then run adaptive), so a thinking-off
request (#1800) sends the control measured for that exact model in
`_THINKING_OFF_CONTROL`: `thinking: {"type": "disabled"}` on opus-5,
`output_config.effort: "low"` on opus-5-5 and fable-5-1 (it lowers, not
removes, thinking on fable-5-1), and no key on any unmeasured model, since
an unmeasured control could 400 every call. A thinking-off request is every
tool-less call, whatever the mode, and a tool call under `off`, an unknown
mode, a starvation guard or the forced-tool refusal. `disabled` is withheld
(no key) when the history echoes thinking blocks. Matrix and ceilings:
`docs/reference/llm-model-capabilities.md` §"Anthropic request shape".

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
`AGENT_PROVIDER_TIMEOUT_OVERRIDES`). The turn route binds it as a deadline and
applies an `asyncio.wait_for` to the turn's **preparation only**
(`InvestigationService.prepare_turn`, which commits nothing); the turn's one
commit (`commit_turn`) runs outside the `wait_for`, because a cancellation that
lands inside a commit leaves its outcome unknown and one that lands after it
answers 504 for a committed turn (#1882). Before it starts, `commit_turn` checks
that at least `TURN_COMMIT_RESERVE_SECONDS` of the deadline is left and answers
the same 504 `REQUEST_TIMEOUT` when it is not, with nothing committed; once
started, the commit runs to its end, bounded only by the database's command
timeout: a database that stops answering mid-commit fails the turn with a 500
and the outcome unknown (#2280; a keyed retry replays the receipt). The
reserve (3.5 s) is sized from the
measured p99 of the commit on SQLite and PostgreSQL times a safety factor of
10, and **the LLM steps pay for it**: `spendable_turn_budget` and
`backstop_turn_budget` both subtract it. After the commit the response path
holds only CPU-bound steps and the route's auto-title, which stays awaited (it
must land before the client can send the next turn, whose full-row save would
otherwise write the placeholder back) and is bounded by
`AUTO_TITLE_TIMEOUT_SECONDS` (15 s, and only while the case still carries its
placeholder title); the upload links (`mark_linked`) run in a background task
the response does not wait for. So a client's own timeout must exceed
the ceiling + the commit reserve + 15 s + its network margin; the server
publishes that sum, a NOMINAL bound (it omits the pre-deadline lookups and
the commit's actual duration, which the margin covers), as
`limits.turnResponseBoundSeconds` on
`GET /api/v1/meta/capabilities` (`config/turn_ceiling.resolve_turn_ceiling`,
resolved per request for the chat provider in force), and clients size their
timeout from it rather than restating the arithmetic (#1905). Every
`AGENT_PROVIDER_TIMEOUT_OVERRIDES` value is held to `AGENT_REQUEST_TIMEOUT`'s
30–600 s (#1905), so the published ceiling is at most 600 s whatever the
provider. The ladder
inside the preparation (`LLMErrorHandler.with_retry`) has two lengths, keyed
on the failure's SHAPE, never its transport (#1294). A **deadline-shaped**
failure (`is_deadline_shaped`: a `TimeoutError` anywhere on the `__cause__`
chain, or a 504 or 524 status on any exception that carries one) spent a whole
deadline and learned nothing, so it gets `max_deadline_retries=1` retry: `2T +
2s`, the pure deadline-shaped cost. Every other retryable failure keeps
`max_retries=3`, and `3T + 14s` bounds every ladder, a mixed one included (a
timeout, then slow 503s). `T` is the resolved per-call ceiling —
`max(LLM_REQUEST_TIMEOUT, LLM_PROVIDER_TIMEOUT_OVERRIDES[provider])`, NOT
`LLM_REQUEST_TIMEOUT` alone, which understates the shipped cluster shape
threefold. Three PAID attempts on the long ladder, because the router's breaker
opens on the third failure and short-circuits the fourth, plus the three
backoffs (2 + 4 + 8; the eight seconds are spent before the attempt the breaker
refuses); the deadline ladder stays under the breaker. On a turn with tools
the tool loop's own attempt comes first, outside the ladder
(`ToolCallingUnsupportedError`, chained from the provider's failure), and the
fallback ladder then makes its own attempts: that turn pays the tool attempt
and then the ladder, bounded by the turn budget. Telling the ladder about the
paid attempt is #2319. Measured on a virtual clock against a hung provider,
before → after: a ladder-only turn 96 → 62 s at T = 30 s and a 120 s turn,
104 → 62 s at 240 s, and 120 s both at T = 120 s, A = 240 s; a turn with tools
92 → 92 s at 120 s, 96 → 92 s at 240 s, and 235.5 s both at T = 120 s. The cost
moves to the breaker. At the shipped timeouts (T = 30 s, A = 120 s), a hung
provider (every attempt a timeout) reaches two breaker failures of three on a
ladder-only turn, so that turn ends `RETRY_EXHAUSTED` (where the full ladder
fit, it ended `PROVIDER_CIRCUIT_OPEN`; both 503 + `Retry-After: 30`), the next
turn pays one `T` and opens it, and the one after is refused at once — 62 + 32 +
0 s over three turns where it was 96 + 0 + 0 (#1513). A turn with tools reaches
three failures (the tool loop's attempt, then the ladder's two) and opens it in
its first turn (92 + 0 + 0 s). Where the turn affords fewer attempts (T = 120 s,
A = 240 s), each turn pays fewer and the breaker opens a turn or two later. A
provider that fails fast (a refused connection) is status-shaped, and opens it
inside its first ladder. The two settings
live in different classes with their own per-provider maps and nothing related
them, so the turn used to be cancelled mid-ladder: the classification never ran
and the caller got an opaque 504 instead of the honest 503 + `Retry-After`
(#1278/#1292). `core/investigation/turn_budget.py` binds the deadline in a
`ContextVar` at the one site that applies the `wait_for`.
`LLMRouter._resolve_timeout` then **clamps every router-borne call's own
timeout** to what is left (less both reserves) — which bounds intent
classification and KB/document synthesis too, not just the ladder, and keeps a
cut-short call on the path that records a circuit-breaker failure (an outer
`wait_for` would cancel it, and `CancelledError` is invisible to
`call_external`'s handlers). `with_retry` adds a later backstop clamp for
providers reached without the router, and **refuses a retry** whose backoff plus
another attempt of the worst cost observed *in that ladder* would not fit.
Refusing the ladder's last iteration reports `RETRY_EXHAUSTED` (nothing that
could have changed the answer was lost); refusing an earlier one reports
`TURN_BUDGET_EXHAUSTED`, whose remediation is the config — a hung provider's
one retry, refused, is such an earlier one. Both → 503. **Unbound
context means unbounded**: outside a turn the budget reads `None` and every
check is inert, which is why the binding is the seam the whole guard hangs on
and has its own test. Whether a deployment's two timeouts actually fit — each
whole ladder plus the step reserve plus the commit reserve inside
`AGENT_REQUEST_TIMEOUT`, so at a 120 s turn `LLM_REQUEST_TIMEOUT` ≤ 33 s for the
long ladder and ≤ 56 s for the deadline one — is reported by
`GET /admin/config/status` as `llm_retry_ladder_fits_turn_budget`, true only
when both fit (`config/retry_budget.model_retry_ladders`, which reads the same
`RetryConfig` the running ladder does).

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
rather than routing, on the provider `_generate()` signatures underneath it.
Both default to absent, and where a call site passes neither the shape-based
provider defaults above are what runs. **Five call sites ship declaring
them**, four `EXTRACTION` and one `INFERENCE`:

| Call site | Declares |
|-----------|----------|
| `core/investigation/intent_resolver.py` (intent classifier) | `reasoning_intent=EXTRACTION`, `min_output_tokens=CLASSIFIER_MIN_OUTPUT_TOKENS` |
| `modules/agent/tools/document_qa_tool.py` (KB/doc answer synthesis) | `reasoning_intent=EXTRACTION` |
| `modules/agent/domain/services/out_of_band.py` (out-of-band triage, #1329) | `reasoning_intent=EXTRACTION`, `min_output_tokens=TRIAGE_MIN_OUTPUT_TOKENS` |
| `modules/agent/domain/services/out_of_band.py` (out-of-band answer, #1329) | `reasoning_intent=EXTRACTION` |
| `core/investigation/milestone_engine/turn_generation.py` (tool-less single-shot diagnostic turn, fm#1116) | `reasoning_intent=INFERENCE`, `min_output_tokens=TOOLLESS_INFERENCE_OUTPUT_FLOOR` |

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

1. Implement the provider in `infrastructure/llm/providers/`, inheriting from `BaseLLMProvider` in `base.py`. Implement **`_generate`** (one request to your API); a provider has no public send method. Every send, routed through the registry or direct (the DA provider, the connection check), goes through `infrastructure/llm/dispatch.py`'s `send_to_model(provider, ...)` (#1998). It checks that model-bound content was minted by `infrastructure/security/model_content.py` (the prompt, the message grammar, the `system`/`preamble` keywords) and that every keyword is in `GENERATE_KWARGS` (= `FORWARDED_KNOB_KWARGS` ∪ `PROVIDER_API_KWARGS` ∪ `PROVIDER_OPTION_KWARGS`, disjoint parts in `base.py`; `ROUTER_CONSUMED_KWARGS` is never in it), REFUSES what is not before any network I/O — `RawModelContentRefused` (`faultmaven/exceptions.py`; a `TypeError`, `retryable=False`), counted once per (argument, kind) by `llm_model_content_violations_total{target,argument,kind}`; there is no warn path (#1998 P3) — then calls `_generate` with plain `str`/`dict` values, every argument by keyword. Declare a class-level `LABEL = "<name>"` and have `provider_name` return it: the dispatch labels its refusals with `LABEL`, read statically from the class (a property would run while it holds the call's content), and it must equal the `provider` your responses carry. `stop_sequences` is content like `system`: every element a minted `ModelText`. A provider-only keyword your provider reads goes into `PROVIDER_OPTION_KWARGS`; `tests/unit/infrastructure/llm/providers/test_provider_boundary_1998.py` fails until it is there. Overriding or adding any other provider attribute bypasses nothing, because the dispatch calls only `_generate`; nothing outside the adapters' own files and `dispatch.py` may name `_generate` or an adapter's transport coroutines, and nothing outside `dispatch.py` may rebind `send_to_model` (`tests/unit/architecture/test_model_dispatch_lexical_1998.py`). `LLMRouter.route` runs the same check at entry, before its `SANITIZE_PII` pass, without unwrapping, so a raw routed call is refused there whether it would reach a dispatch, a cache hit or a test's registry double. Production code never calls a router's `route` itself: it calls `dispatch.route_model_call(router, ...)` (#1998 P2), a plain function that checks the content before any router (a test's double included), refuses a raw send or a keyword `route()` does not declare (`ROUTE_KWARGS`) at the caller's line, and returns the router's coroutine. Rule S of the dispatch lexical test flags `.route` read outside `dispatch.py`, S2 flags a registry's `route_request` read outside `router.py`, and A3 keeps `route_model_call` and `send_to_model` imported under their own names and only ever called, so the propagation census sees every send. A refusal is a programming error, never a reason to fall back: every handler between a send site and its request or job boundary lets it through (`except RawModelContentRefused: raise` ahead of a broad handler), `tests/unit/architecture/test_model_refusal_propagates_1998.py` drives every send-site function from its boundary and fails on one that swallows it, and a route answers it as a 500 `SERVICE_ERROR`, never as the AI provider's failure (`api/exception_handlers.py`). In the test suite a raw send raises at the send. A test that drives a provider or the router mints its input with `tests/model_content.py` (`vetted`, `vetted_messages`, `vetted_kwargs`); one that sends unminted content on purpose asserts the refusal, anchored: `pytest.raises(RawModelContentRefused, match=refusal_pattern(target, summary))`. The check accepts exact builtins only (`list`, `dict`, `str`, `ModelText`), and the registry re-raises a refusal without counting a provider failure. The reasoning artifacts an adapter echoes (`provider_metadata`) are a content channel: every `str` in them, leaf or dict key, is minted by `model_output`, and unwrapped before `_generate`.
2. Merge leftover kwargs into the request body with **`self._merge_extra_kwargs(payload, kwargs, model=...)`**, never a hand-rolled `payload.update(kwargs)` — the router-level knobs (`ROUTER_LEVEL_KWARGS` in `providers/base.py`: `ROUTER_CONSUMED_KWARGS` = `case_id`, `bypass_cache`, `provider_override`, `data_type`, never forwarded on a routed call; `FORWARDED_KNOB_KWARGS` = `cache_prompt`, `reasoning_intent`, `min_output_tokens`, forwarded and translated or popped by the provider) are not API fields, and a provider that merges them raw sends them as body fields and is rejected by any OpenAI-compatible endpoint — also on the direct-call path (`DA_PROVIDER`), where nothing upstream strips them (#2002). The DA tool loop therefore builds its direct-call kwargs without `case_id` and `provider_override` (router knobs a concrete provider never needs), and the discard is the provider-side defence behind it. Consume a knob (pop it) only if the provider actually translates it. A new `LLMRouter.route` keyword must be added to one of `ROUTER_CONSUMED_KWARGS`, `FORWARDED_KNOB_KWARGS` or `PROVIDER_API_KWARGS`; `test_router_knobs_never_reach_a_provider.py` pins the three constants to `route()`'s signature. The router forwards `response_format`, `tools`, `tool_choice` and `messages` as `None` on a call that did not set them, so an adapter tests their truthiness, never their presence (`"response_format" in kwargs` put `null` in three bodies and `format: "json"` on every Ollama call).
3. Populate `stop_reason` via `normalize_stop_reason()` (see "Stop reasons and truncation") — `UNKNOWN` is the honest default, not `STOP`.
4. Register in `infrastructure/llm/providers/registry.py`.
5. Add config in `config/settings.py` and document it in `.env.example` (`scripts/check_env_example_sync.py` checks the three default-model declarations agree).
6. Add tests in `tests/unit/infrastructure/` — `tests/unit/infrastructure/llm/test_router_knobs_never_reach_a_provider.py` derives its matrix from `PROVIDER_SCHEMA` (add the provider's response double to `_RESPONSE_BY_PROVIDER`), so a newly registered provider is checked for router-knob leaks automatically; `tests/unit/infrastructure/llm/providers/test_reasoning_intent.py` does the same for the reasoning knobs.
7. Add a row to the provider table above (and a pricing entry in `infrastructure/llm/pricing.py` if the model is billed).

Long-form guide: `docs/guides/adding-llm-providers.md`. Capability detection
design: `docs/architecture/core-architecture/structured-output-capability-system.md`.

## Diagnosing a provider locally

`docs/development/local-troubleshooting.md` §LLM provider issues — the resolved
role routing is read from `GET /api/v1/admin/llm/config`, never by echoing keys.
