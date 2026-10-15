# Case-Scoped PII Redaction Architecture

## Context

FaultMaven is a SaaS product where user log data crosses a trust boundary: user uploads data (logs, configs, traces) which FaultMaven sends to external LLM providers for analysis. PII in this data (IPs, emails, hostnames, API keys) must not reach external APIs.

However, FaultMaven is a troubleshooting tool. The user needs to see real values in responses. Naive redaction that strips IPs also strips the data users are investigating.

This creates three requirements:

1. **LLM never sees raw PII** — all content sent to external providers must be redacted
2. **User always sees real values** — responses must contain original IPs, emails, etc.
3. **Cross-file consistency** — the same IP must get the same placeholder across all evidence files in a case, or the LLM cannot correlate across files

## Problems Solved

### 1. Cross-File Placeholder Collision

**Before:** `DataSanitizer._sanitize_text()` creates a fresh `entity_registry` on every `sanitize()` call. Two files with different IPs both get `<IP_ADDRESS_1>` — the LLM concludes they are the same entity.

**After:** A single `CaseRedactionContext` persists the registry across all `sanitize()` calls within a case. IP `10.0.0.1` always maps to `<IP_ADDRESS_1>`, IP `10.0.0.2` always maps to `<IP_ADDRESS_2>`, regardless of which file they appear in or which turn processes them.

### 2. Tool Results Bypass Redaction

**Before:** `search_file` and `deep_analysis` read raw unredacted file content and pass it directly to the LLM via the tool execution loop. Even if the prompt was redacted, the LLM saw raw PII through tool results.

**After:** Tool results are redacted with the same case-scoped registry before being appended to the LLM conversation. The registry ensures consistency — an IP that appeared in the prompt as `<IP_ADDRESS_1>` also appears as `<IP_ADDRESS_1>` in tool results.

### 3. No Reverse-Substitution

**Before:** The LLM responded with `<IP_ADDRESS_1>` placeholders. These were returned to the user as-is. Users saw placeholders instead of real values.

**After:** `InvestigationService.prepare_turn()` calls `redaction_ctx.reverse()` on the agent response before returning it to the user, in its `_absorb_engine_result` phase (`investigation_service/turn_results.py`). The user sees original values.

## Architecture

Redaction is a **case-scoped, LLM-boundary concern** managed by the MilestoneEngine (which has `case_id`), not the Router (which is a generic LLM abstraction).

```text
User uploads file
    → stored raw (never redacted at rest)
                                            ↓
InvestigationService.prepare_turn()
  ├─ Preprocessing (classify_and_extract) builds the file's summary and
  │  structural index from the raw content; both are persisted raw
  ├─ load_case_redaction(): the turn's ONE CaseRedactionContext, its registry
  │  loaded from Redis (#1956)
  └─ intent classifier / out-of-band triage and answer: sent through it
                                            ↓
MilestoneEngine._process_turn_impl()      (handed the same context)
  ├─ Context builder assembles the prompt from evidence, uploads, user message, history (raw)
  ├─ Redact the prompt with the case-scoped registry              (StructuredOutputGenerator)
  ├─ Send to LLM
  ├─ LLM calls tool → execute → redact result with SAME registry → return to LLM
  │  (a tool that calls a model itself — kb_qa, case_evidence_search,
  │  deep_analysis — sends through the SAME context, via ToolContext)
  ├─ LLM responds with placeholders
  └─ Save registry to Redis                                       (_finalize_turn)
                                            ↓
InvestigationService.prepare_turn()
  ├─ Save the registry again if anything after the engine grew it (a save with
  │  nothing new writes nothing)
  ├─ turn_results._absorb_engine_result: reverse-substitute placeholders → original values
  │  (every route's reply: engine, terminal Q&A, out-of-band)
  └─ turn_response._build_turn_response: build the TurnResponse (real IPs, names, etc.)
                                            ↓
InvestigationService.commit_turn()
  └─ the turn's one commit (#1882), then the TurnResponse is returned
```

Redaction happens at one boundary: where content leaves for an LLM. Everything upstream (the upload, the summary and structural index preprocessing builds from it, the evidence) is stored and assembled raw. The engine redacts the assembled prompt and every tool result with the case-scoped registry, and every other model call made on the turn's behalf sends through the same context, so one consistent namespace covers all the PII that reaches the model, whichever file, turn or call it came from.

### Why MilestoneEngine, Not Router?

The Router is a generic LLM abstraction — it routes requests to providers, handles caching, manages fallback chains. It has no concept of cases or evidence files. Putting case-scoped logic in the Router would violate separation of concerns.

The MilestoneEngine owns the case lifecycle. It has `case_id`, manages the tool loop, and controls what content reaches the LLM. It is the natural owner of the redaction boundary.

The Router retains its existing `_sanitize_if_needed()` as a safety net for other LLM calls. For investigation turns and the knowledge-authoring paths (below), the prompt is already redacted at the engine layer, and the Router's sanitizer sees only placeholders — which don't match any PII pattern, so they pass through unchanged.

## Components

### CaseRedactionContext

**File:** `infrastructure/security/case_redaction.py`

A bidirectional PII registry scoped to a single investigation case. Backed by Redis for cross-turn persistence.

```python
class CaseRedactionContext:
    _forward: Dict[str, Dict[str, str]]  # entity_type → {value → placeholder}
    _reverse: Dict[str, str]             # placeholder → original value

    async def load()       # Load registry from Redis
    async def save()       # Read the stored registry, merge missing entries in (HSETNX), refresh the TTL
    def sanitize(text)     # Redact PII using case-scoped registry
    def reverse(text)      # Replace placeholders with originals
    async def cleanup()    # Delete Redis key (case closed)
```

**Key behaviors:**

- `sanitize()` delegates PII detection to `DataSanitizer.sanitize_text_with_registry()` but passes the case-scoped `_forward` registry so the same value always gets the same placeholder
- `reverse()` sorts placeholders by length descending to avoid partial replacement (`<IP_ADDRESS_10>` before `<IP_ADDRESS_1>`)
- Redis key: `redaction:registry:{case_id}`, a HASH: field `json.dumps([entity_type, value])`, value the placeholder. TTL: configurable (default 7 days), refreshed on every save of a case that has entries — so an active case's registry lives the TTL past its last turn
- If Redis is unavailable: works in-memory (consistent within turn, not across turns)
- If `enabled=False`: both methods return input unchanged (zero overhead)
- `save()` MERGES (#1956). It reads the stored registry into the context, then writes each entry the context holds that is not stored with `HSETNX`, and refreshes the TTL with `EXPIRE`, the writes and the `EXPIRE` in one MULTI/EXEC transaction (a dropped connection cannot leave a registry with no TTL). The stored registry only grows, and an entry already there is never replaced, so contexts on one case key — two concurrent turns, a turn and the case title, a context whose `load()` failed — never drop each other's entries, in any save order, with no lock and no retry: Redis writes each field atomically. Reading first also restores entries the key lost when it expired under a live context. A save with nothing new writes no field but still refreshes the TTL. It used to write the context's whole registry as one JSON value, so the later of two saves erased the other's new entries.

Built through one loader, `load_case_redaction(case_id, sanitizer, redis_client)` (enabled by `should_redact()`, TTL from `redaction_registry_ttl_hours`); the send sites use two helpers, `case_redacted(text, ctx)` and `case_redacted_messages(messages, ctx)`, which pass through when there is no context.

### DataSanitizer.sanitize_text_with_registry()

**File:** `infrastructure/security/redaction.py`

A new method on `DataSanitizer` identical to `_sanitize_text()` but accepting an external `entity_registry` instead of creating a fresh one. This is the integration point between case-scoped redaction and the existing PII detection pipeline (regex patterns + Presidio NLP).

The existing `sanitize()` and `_sanitize_text()` methods are unchanged.

### DataSanitizer Detection Pipeline

The sanitizer has two detection stages, both configurable through `ProtectionSettings`:

**Stage 1 — Regex patterns** (`regex_patterns`, which is `SANITIZER_PATTERNS`): `SECRET_PATTERNS`, the one secret-detection set (#1980), then `NETWORK_ADDRESS_PATTERNS` (internal IPs, MAC addresses). The secrets: private keys (a block without its END line too), connection URLs with credentials (postgres, mysql, mariadb, mongodb(+srv), redis(s), amqp(s), and `user:password@` in any other URL), GitHub and Slack tokens, `sk-`/`pk-` keys, AWS key ids and context-anchored secret keys, JWTs, `Bearer` tokens, `Authorization: Basic` credentials, and `key <separator> value` for the password, secret, token and API-key families. The knowledge document preprocessor detects with `SECRET_PATTERNS` too, so a document and case content are scanned for the same secrets.

- Detection runs every pattern on the raw text, and the matches are merged into non-overlapping spans before anything is substituted (`find_redaction_spans`): what one path writes in place of a secret cannot change what is detected, and no placeholder wraps another.
- Then it runs again on the text with each span masked by a placeholder-shaped stand-in, until the spans stop growing, so one pass finds what a second pass over its own output would: redaction is idempotent, on the sanitizer's path and the preprocessor's alike. A match can need its neighbour gone — in `10.0.0.5password=hunter2` the keyword is glued to the address, and only once the address is a `<placeholder>` is it a key. Real text settles within three rounds; a crafted chain is cut at `_MAX_MASKED_ROUNDS` and each line still growing is redacted whole. Spans that only touch stay two.
- A keyed secret's span is its **value**: the key stays in clear (`id_token=<…>`), and so do a quoted value's quotes when they delimit it (`{"password": "<…>"}` is still JSON). An unquoted value is the whole run up to whitespace, as on main: an escaped `\n` inside it and closing brackets or quotes at its end are part of it (`{"msg": "password: x"}` loses its `"}`), so no tail of a secret is ever left in clear. A value that is a provider token (a JWT, an `sk-` key, a GitHub token) is typed by that token's own pattern, so one secret has one placeholder however it is spelled — bare, after `Bearer`, as `id_token=`, as `"access_token":`.
- A keyed secret is read after `=`, `=>`, `:` (`key:value`, `key : value`, `key: value`) or as a quoted key, optionally after `_`/`-` (`password_=`), on the same line. On the next line only in two cases: after `=` with nothing after it, the next line's first token, as main read it (`password=` then the value) — unless that line starts a key of its own; and after `key:` with nothing after it, a YAML continuation — the next line indented deeper than the key's line, and not a comment, a key or a list item — so `Enter password:` followed by a log line, or `token:` followed by `  # filled in by CI`, reads nothing. Any value is a secret except, as the WHOLE value, a word from a closed list kept minimal — each entry cited by a negative line that needs it (`yes`/`no`, `true`/`false`, `null`, `required`, `expired`, `n/a`, `incorrect`, `invalid_grant`, `changed`, `rotated`, `missing`, `not`, the type names `str` and `Optional[str]`, a masked `***…`, an empty `""`): `password: changeme` mid-sentence is a password, and `(using password: YES)` is not. A compound key — the keyword after any letter, digit or underscore (`db_password`, `clientSecret`, `PGPASSWORD`, `xpassword`) — takes the same separators but never a count of up to five digits, so `failed_password: 520` survives intact. A value that refers to a secret is not one, again only as the whole value (`password=${{x` is a secret): `${VAR}`, `${{ … }}`, a command substitution `$(…)`, `{{ … }}`, `<your-password>`, a placeholder or marker a pass wrote (matched in the case it is written in), a SQL bind (`?`, `$2`, `:pwd`, `%s`), for `pwd` a path, and for a secret or token a Kubernetes `namespace/name`. An escaped newline, tab or return (case-sensitive: `\N` escapes nothing), or a percent-encoded byte, before a key or a token is a boundary, as the character it encodes would be.
- Every pattern runs in time linear in the text: possessive or atomic quantifiers where one could re-split what it read, and no attempt reads past where the next would start. The growth tests in `tests/unit/infrastructure/security/test_secret_patterns_1980.py` pin each hostile shape found in review.

**Stage 2 — Presidio NLP** (`_apply_presidio()`): Sends text to Presidio Analyzer/Anonymizer services for NLP-based entity detection. Two settings control behavior:

- `entities_to_protect` — which entity types Presidio detects. Default excludes `PERSON`, `DATE_TIME`, `NRP`, `LOCATION`, and `URL` because spaCy NER (trained on prose) produces false positives on machine-generated log data (month abbreviations, hostnames, and syslog fields misclassified as person names).
- `min_score_threshold` — minimum confidence for Presidio detections (default: 0.85). Higher thresholds reduce false positives at the cost of missing some true PII.

Both values are read from `ProtectionSettings` in the constructor. The constructor always resolves settings via `get_settings()` if none are injected.

### One Context Per Turn

**File:** `modules/agent/domain/services/investigation_service/service.py`

`InvestigationService.prepare_turn()` loads the turn's context once, through `load_case_redaction()`, and hands the SAME instance to every model call it makes on the turn's behalf (#1956): the intent classifier (`IntentResolver.resolve`), the out-of-band triage and answer, and `MilestoneEngine.process_turn(redaction_ctx=...)`, which passes it to the turn's generation, the terminal Q&A path and the `ToolContext` its tools run with. One load, one registry, one reverse. Two contexts on one case key would each hold a snapshot, and whichever saved last would drop the other's new entries. A direct engine call (no context handed in) loads the case's own through the same loader.

### Model Calls on a Turn's Behalf

Every send site redacts what it sends through the turn's context, at the send: `messages = await case_redacted_messages(..., redaction_ctx)`, then `route(messages=messages)`. A repo-wide AST guard (`tests/unit/architecture/test_llm_send_sites_are_case_redacted_1956.py`) classifies every `.route(`/`.generate(` call as redacted at the site or allowlisted with a reason, and fails on anything else.

| Call | Carries | Reversed |
| --- | --- | --- |
| `case_evidence_search` / `kb_qa` synthesis (`DocumentQATool.answer_question`) | case evidence chunks (stored raw) / KB chunks | in the engine's final reply, from the registry the call filled |
| `deep_analysis` (`LocalTier2Service.analyze`) | raw file sections | same |
| Intent classifier (`IntentResolver._classify`) | the user's typed message | nothing to reverse (a digit) |
| Out-of-band triage and answer (`out_of_band.py`) | the user's message and the last answer | the answer, by the service with the turn's context |
| Case title (`title_generation._generate_title_with_llm`) | the case's user text | **never** (below) |

These all reach the model through the router (`llm_provider`); none goes to the dedicated `DA_PROVIDER`. What skipping the case pass costs is the registry, not the text: the router's own pass under `SANITIZE_PII` produces the same text, but a value it alone redacts never enters the case registry, so its placeholder in an answer cannot be reversed for the user — and protection would rest on whichever router `LLM_ROUTER_CLASS` configures.

**Titles are never reversed.** A title is shown to every reader of the case, in every list, so a real value restored into it would be a disclosure. The title builds its own context through the loader, after the turn's save (it runs once the turn has committed), and sends its prompt through it; `is_title_valid` refuses a title carrying a placeholder anywhere, in any letter case, which sends it to the extractive fallback.

**No title carries what the sanitizer detects (#1967).** This holds on every title path (smart-extractive, LLM, fallback) and whatever `SANITIZE_PII` says. Two steps in `modules/case/api/title_generation.py` enforce it:

- **Sources.** Every title source (problem statement, evidence and file summaries, chat, description, the regenerate route's `hint`) passes `_scrub_title_source` first. That runs the sanitizer's in-process regex half on the raw text. The raw text comes first because a credential is detectable only whole: when `_sanitize_title_content` ran first, its email and path patterns cut credentials into fragments that nothing then matched. Each placeholder then becomes a type marker (`[password]`), and `_sanitize_title_content` runs last.
- **Candidates.** Every candidate then passes `_passes_disclosure_gate`, the sanitizer's full detection, with Presidio when the analyzer answers. This happens where a candidate is validated, so the fallback still runs, and once more right before the write. One generation shares a verdict memo, so each distinct candidate costs one detection.

A deployment whose posture is fail-open (`SANITIZE_PII=false` or `PROTECTION_FAIL_OPEN=true`) accepts regex-only detection while the analyzer errors, as every other redaction path does. Otherwise the gate fails closed. If the sanitizer is missing, raises, or answers with something that is not text, no title is written, and `POST /cases/{id}/title` answers 503.

### MilestoneEngine Integration

**Files:** `core/investigation/milestone_engine/turn_generation.py` (the main turn path) and
`core/investigation/milestone_engine/terminal_turns.py` (`TerminalTurnHandler`,
the terminal Q&A path) — both are handed the turn's context and build none of
their own. The `StructuredOutputGenerator` collaborator (`generation.py`)
redacts the prompt and tool results within the shape it owns, through
`case_redacted`.

The engine manages the redaction lifecycle across `_process_turn_impl()` and its
phases:

1. **Receive context** — from the service (or, on a direct engine call, loaded through `load_case_redaction()`), passed to `_generate_turn_response` (`turn_generation.py`) and the terminal Q&A path
2. **Redact prompt** — at the entry to `StructuredOutputGenerator.generate_structured_output()`, covering both DA (tool-augmented) and single-shot paths
3. **Redact tool results** — in `StructuredOutputGenerator._tool_augmented_generate()` after `_format_tool_result()` and before truncation/append
4. **Save registry** — after LLM call completes, before returning result, in `_finalize_turn` (`turn_completion.py`). It is not part of the turn's commit and need not be: a pseudonym is a keyed function of the value, so mappings left by a turn that then fails are a harmless superset (#1882)
5. **Return context** — threaded through `_finalize_turn` as a phase input/output, then included in the result dict

`should_redact()` is the one copy of the decision: a sanitizer is configured (the DI container hands one out) AND `SANITIZE_PII` is on. When it is `False`, `CaseRedactionContext` is created with `enabled=False` and all operations are no-ops. Every path that sends case or document text to a model reads it — `test_every_redaction_context_is_enabled_by_should_redact` fails on a context built any other way; the only constructions are `load_case_redaction()` and `model_boundary_redaction()`.

### InvestigationService Integration

**File:** `modules/agent/domain/services/investigation_service/turn_results.py`

The service sets the turn's context on every route's result, so the one reverse below covers the engine's reply, the terminal Q&A answer and the out-of-band answer alike. **Response reverse-substitution** happens in `prepare_turn`'s `_absorb_engine_result` phase (`modules/agent/domain/services/investigation_service/turn_results.py`), before the assistant row is appended:

```python
redaction_ctx = result.get("redaction_ctx")
if redaction_ctx:
    agent_response_text = redaction_ctx.reverse(agent_response_text)
```

### Knowledge-Authoring Paths

**Files:** `modules/knowledge/domain/services/conversion_service/` and `document_preprocessor.py` (case→runbook and document→runbook conversion)

The paths that turn a case or an uploaded document into a runbook send that text to a model too, and apply the same rule at the same layer (#1661, #1901). Each builds its context through `model_boundary_redaction(sanitizer)` in `infrastructure/security/case_redaction.py`: the same class, over the same injected sanitizer instance the engine holds, enabled by `should_redact()`. It takes no case or conversion id: the context keeps no registry, so an id would key nothing, and placeholders are a keyed function of the value (#971), so they match the investigation's without one.

Every model call is covered: conversion's triage, analysis and per-failure-mode generation. Each redacts its whole outbound message list before the truncation retry, so a retry resends the redacted text, and outside any catch-all, so a `RedactionUnavailableError` stops the run with nothing sent — triage's fail-open guard and conversion's per-failure-mode error handling do not absorb it. A multi-mode conversion redacts every failure mode's request before it sends any (`ConversionService._prepare_conversion`), so a refusal on the third of six modes sends no generation and writes no draft, rather than leaving the first two behind. Only text content is redacted: `CaseRedactionContext.asanitize_messages` refuses a message whose content is not a string (`TypeError`) instead of sending it unredacted.

It matters independently of the router. `LLMRouter._sanitize_if_needed()` runs its own pass under the same flag, so a default deployment with `SANITIZE_PII=true` already sent this text redacted — but that pass is a property of the default router, which `LLM_ROUTER_CLASS` can substitute.

No registry is loaded or saved, and nothing is reversed. The investigation path reverses placeholders in the reply it shows the user; a runbook is de-identified knowledge, so the placeholders the model writes are what is persisted — the same text the default router's own pass produced before this layer existed.

### Knowledge Item Titles

**Files:** `modules/knowledge/domain/item_title.py`, `knowledge_service.py` (`ingest_runbook`, `upload_document`, `update_document_metadata`), `conversion_service/` (`service.py`, `job_persistence.py`, `runbook_scan.py`), `ingestion.py`

The rule: **no text derived from a runbook title reaches any reader, log or transcript unscrubbed, on any create, convert, verify or update path** (#1981). A title is shown to every reader of the tier it lives in (team and global included) and is never reversed. So it is redacted with the DI sanitizer **once, where it enters as raw text**, the scrubbed value is assigned back to the object it came in on, and everything else is derived from that value: the draft's id slug, its file name, the text of a runbook made from a template, the stored analysis, the API response, the log line and the case-transcript notice. Scrubbing a derived copy does nothing: a slug of `password=hunter2` is `...-password-hunter2`, which has no `=` left for a pattern to match. One helper, `sanitize_item_title`, is called at each point of entry:

- `ingest_runbook`, the one place a `knowledge_items` row is created (uploads, published drafts and the KB pack all reach it), and `upload_document`, which scrubs first because it writes the upload and draft rows from the title before `ingest_runbook` runs;
- `update_document_metadata`;
- `ConversionService`: a failure mode's title as soon as the model has written it (`convert_document`), a case's title as it arrives (`convert_from_case`), the title the model writes into a case draft's frontmatter (rewritten before the id is minted from it), the title typed into `create_runbook_from_template`, and `verify_draft`. `_persist_job_rows`, the one place a `conversion_drafts` row is written, redacts and cuts the title to the column and assigns the stored value back to the draft it returns, so the object and the row are one text. It is the backstop for the paths above and the entry for the disk scan, which derives nothing from a file's title first;
- the dormant `KnowledgeIngester` entry points.

A draft is not author-only. `GET /knowledge/drafts` lists every global-scope draft to every user, and a case's drafts are listed to its readers (`GET /cases/{id}/reports`), so a draft's title is disclosed as soon as its row exists, well before it is published.

The helper runs the sanitizer to a fixed point (at most six passes), so every copy of a title is one text whatever the sanitizer's own idempotence. The regex half has it since #1980 — a detection that needs its neighbour gone (`10.0.0.5password=hunter2`; the deepest, password waiting on IP waiting on MAC) settles inside one call — so such a title takes two calls: one that changes it and one that sees nothing change. A sanitizer that has not settled, that raises (`RedactionUnavailableError` under a fail-closed posture, `PseudonymKeyUnavailableError` in a cloud deployment with no key), that answers with something that is not text, or that is not wired, makes the helper raise `ItemTitleScrubUnavailable`. Every caller runs it before its first write, so nothing is left behind, and the API answers **503** with a generic detail (upload, update, convert, manual create, verify and scan), as `POST /cases/{id}/title` does for the case title gate (#1967). `POST /knowledge/scan` can answer it mid-walk: the files imported before the failing one stay imported. The conversion entry points refuse a deployment with no sanitizer before spending a model call.

The column cut comes **after** the scrub, and the cut text is scrubbed again until the two agree. A secret that straddles the column's length would otherwise be cut into a fragment no pattern matches. The item's title is cut to its `String(512)` at entry, so a title the item cannot hold never fails after the draft, job and upload rows are committed; the draft title and the stored upload file name are cut to their `String(255)`.

The KB pack is the one exemption: `kb_init` passes `shipped=True`, and `ingest_runbook` stores that title as written. A pack title is vendored, reviewed source, not user or model text. A redaction outage that begins after a healthy start (Presidio answering, then failing under a fail-closed posture) would otherwise raise out of the scrub and stop the platform corpus from seeding; a seed that starts with the analyzer already down runs regex-only and does not raise either way. The default is the scrub, and a test pins that no other call site passes `shipped`.

The runbook body is not redacted on create: the quality gate refuses hardcoded credentials in it (`runbook_validator`), and `update_document_metadata` redacts it on edit. Draft rows written before this change are not rewritten; `verify_draft` scrubs the one it publishes.

## Configuration

Two settings control whether redaction is active:

- `PROTECTION_ENABLED=true/false` (default: `false`) — master toggle for protection features
- `SANITIZE_PII=true/false` (default: `false`) — controls PII redaction before LLM calls

Both must be enabled for full PII protection. When both are `false` (the default for standalone, self-hosted deployments), Presidio health checks are skipped entirely at startup — no connection attempts are made to Presidio services.

Additional settings for detection tuning:

- `REDACTION_REGISTRY_TTL_HOURS` — Redis key TTL for case-scoped registry (default: 168 hours / 7 days)
- `MIN_SCORE_THRESHOLD` — Presidio confidence threshold (default: 0.85)
- `ENTITIES_TO_PROTECT` — Presidio entity types to detect (default excludes `PERSON`, `DATE_TIME`, `NRP`, `LOCATION`, `URL` — see Detection Pipeline above)

See [PII Sanitization Configuration](../../operations/security/pii-sanitization-configuration.md) for operational details.

## Edge Cases

### Redis Unavailable

Registry works in-memory for the current turn. Consistent within the turn, not across turns. Logged as a warning. Acceptable degradation — cross-turn consistency is a quality improvement, not a correctness requirement.

### Concurrent Turns on Same Case

The turn's context is loaded by the service, before the engine's per-case lock (`_case_locks`, which is per process anyway), so two concurrent turns on one case (two tabs, two pods) each hold a snapshot. The save is a merge (`HSETNX` per entry), so neither can drop the other's entries: a later turn loads both, and reverses a placeholder either one registered. The same holds for the case title racing the turn after it.

### Single Registry Path

When `SANITIZE_PII=true`, all redaction flows through one `CaseRedactionContext` per turn of a case, applied at the LLM boundary only: the prompt, each tool result, and every other model call made on the turn's behalf. There is no "double redaction". Preprocessing does not redact, and the Router's sanitizer sees only placeholders. When `SANITIZE_PII=false`, no redaction occurs at any layer.

### Not Implemented: Redaction During Preprocessing

Preprocessing does not redact. An upload's persisted summary and structural index hold raw content, as the upload itself does, and are redacted like any other prompt content when they reach an LLM. Redacting at extraction time, so that the persisted preprocessing artifacts carry placeholders rather than values, would be implemented if it is needed in the future. It would have to use the same case registry, or its placeholders would disagree with the ones the inference layer assigns.

### Placeholder in User Message

If a user types `<IP_ADDRESS_1>` in their message, `reverse()` would replace it with the original value. This is the correct behavior — the user is referencing a previously-seen entity.

## Where It Lives

| File | Role |
| --- | --- |
| `infrastructure/security/case_redaction.py` | `CaseRedactionContext`: the case-scoped registry, persisted in Redis |
| `infrastructure/security/redaction.py` | `DataSanitizer`, including `sanitize_text_with_registry()` and the Presidio settings wiring; the regex pattern sets (`SECRET_PATTERNS`, `NETWORK_ADDRESS_PATTERNS`) and `find_redaction_spans()` |
| `infrastructure/security/case_redaction.py` | `should_redact()`, the one redaction decision; `load_case_redaction()`, the case loader; `case_redacted()` / `case_redacted_messages()`, the send-site helpers; `model_boundary_redaction()`, the knowledge-authoring paths' context |
| `modules/agent/domain/services/investigation_service/service.py` | Loads the turn's one context and hands it to every call on the turn's behalf; saves it after the route |
| `core/investigation/milestone_engine/generation.py` | Redacts the prompt and each tool result (`StructuredOutputGenerator`); puts the context on `ToolContext` |
| `core/investigation/milestone_engine/turn_completion.py` | Saves the registry (`_finalize_turn`) |
| `core/investigation/milestone_engine/terminal_turns.py` | The terminal Q&A path, on the turn's context |
| `modules/agent/tools/document_qa_tool.py`, `core/preprocessing/tier2/local_service.py` | `kb_qa` / `case_evidence_search` synthesis and `deep_analysis`, sent through the turn's context |
| `core/investigation/intent_resolver.py`, `modules/agent/domain/services/out_of_band.py` | The intent classifier and the out-of-band triage and answer, sent through the turn's context |
| `modules/case/api/title_generation.py` | The case title: its own context through the loader; never reversed |
| `modules/agent/domain/services/investigation_service/turn_results.py` | Reverse-substitution (`_absorb_engine_result`) |
| `modules/knowledge/domain/services/conversion_service/`, `document_preprocessor.py` | Redact conversion's triage, analysis and generation calls |
| `container/providers/services.py` | Passes the sanitizer and the Redis client to the engine, and the sanitizer to the conversion service |
| `config/settings.py` | `redaction_registry_ttl_hours`, `entities_to_protect`, `min_score_threshold` |

## What Redaction Does Not Touch

- **Preprocessing**: builds summaries and structural indexes from raw content (see *Not Implemented* above)
- **Context builder**: assembles raw content; the engine redacts downstream
- **Router**: its `_sanitize_if_needed()` stays as a safety net for LLM calls the engine layer does not redact
- **Tool implementations**: `search_file` and `deep_analysis` read raw content, and the engine redacts their results; a tool that calls a model itself redacts what it sends through the turn's context
- **Storage**: uploads, evidence and preprocessing artifacts are stored raw, never redacted at rest

## Testing

### Unit Tests

`tests/infrastructure/test_case_redaction.py` — 20 tests covering:

- Same IP across calls → same placeholder
- Different IPs → different placeholders
- `reverse()` restores all originals
- Longest-first replacement ordering
- Redis load/save round-trip
- Redis failure resilience
- `enabled=False` → no-op
- Custom TTL propagation
- `sanitize_text_with_registry()` consistency

`tests/infrastructure/test_redaction.py` — includes regression tests:

- `TestPasswordRegexWordBoundary` (5 tests): verifies compound tokens like `failed_password: 520` survive sanitization intact, while standalone `password=secret` is still redacted

`tests/unit/infrastructure/security/test_secret_patterns_1980.py` pins the pattern set both ways: every listed secret spelling is detected with its entity type on each path (the sanitizer, its regex half, the document preprocessor), and a corpus of ordinary incident text comes back unchanged except for the listed network identifiers.
- `TestPresidioSettingsWiring` (4 tests): verifies settings injection, custom entity list honored, default entity list excludes `PERSON`/`DATE_TIME`/`NRP`/`LOCATION`/`URL`, default threshold is 0.85

### Integration Verification

Playbook scenario S6 (Cross-Evidence Correlation) with `SANITIZE_PII=true`:

- `failed_password: 520` survives intact where the structural index reaches the prompt (no `<PASSWORD_1>` corruption)
- No `<PERSON_N>` false positives on timestamps or log tokens
- IPs correctly redacted to `<IP_ADDRESS_N>` placeholders
- User-facing response shows real IPs (reverse-substituted)
- IPs from Linux_2k.log and OpenSSH_2k.log get different placeholders
- Response quality at parity with `SANITIZE_PII=false` runs
