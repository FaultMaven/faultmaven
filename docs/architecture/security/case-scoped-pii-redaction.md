# Case-Scoped PII Redaction Architecture

## Context

FaultMaven is a SaaS product where user log data crosses a trust boundary: user uploads evidence (logs, configs, traces) which FaultMaven sends to external LLM providers for analysis. PII in this evidence (IPs, emails, hostnames, API keys) must not reach external APIs.

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

**After:** `InvestigationService.process_turn()` calls `redaction_ctx.reverse()` on the agent response before returning it to the user. The user sees original values.

## Architecture

Redaction is a **case-scoped, LLM-boundary concern** managed by the MilestoneEngine (which has `case_id`), not the Router (which is a generic LLM abstraction).

```text
User uploads file
    → stored raw (never redacted at rest)
                                            ↓
InvestigationService.process_turn()
  ├─ Create CaseRedactionContext, load from Redis
  ├─ For each attachment:
  │     PreprocessingService.classify_and_extract(redaction_context=ctx)
  │       └─ Redact structural index with case-scoped registry
  ├─ Save registry to Redis (extraction-layer mappings)
                                            ↓
Context builder assembles prompt from evidence + user message + history
                                            ↓
MilestoneEngine._process_turn_impl()
  ├─ Load CaseRedactionContext from Redis (includes extraction-layer mappings)
  ├─ Redact prompt with case-scoped registry
  ├─ Send to LLM
  ├─ LLM calls tool → execute → redact result with SAME registry → return to LLM
  ├─ LLM responds with placeholders
  └─ Save registry to Redis
                                            ↓
InvestigationService.process_turn()
  ├─ Reverse-substitute placeholders → original values
  └─ Return TurnResponse to user (real IPs, names, etc.)
```

The case-scoped registry flows through **both** the extraction layer (structural index creation) and the inference layer (LLM prompts + tool results), ensuring a single consistent namespace for all PII within a case.

### Why MilestoneEngine, Not Router?

The Router is a generic LLM abstraction — it routes requests to providers, handles caching, manages fallback chains. It has no concept of cases or evidence files. Putting case-scoped logic in the Router would violate separation of concerns.

The MilestoneEngine owns the case lifecycle. It has `case_id`, manages the tool loop, and controls what content reaches the LLM. It is the natural owner of the redaction boundary.

The Router retains its existing `_sanitize_if_needed()` as a safety net for non-investigation LLM calls. For investigation turns, the prompt is already redacted by the engine, and the Router's sanitizer sees only placeholders — which don't match any PII pattern, so they pass through unchanged.

## Components

### CaseRedactionContext

**File:** `infrastructure/security/case_redaction.py`

A bidirectional PII registry scoped to a single investigation case. Backed by Redis for cross-turn persistence.

```python
class CaseRedactionContext:
    _forward: Dict[str, Dict[str, str]]  # entity_type → {value → placeholder}
    _reverse: Dict[str, str]             # placeholder → original value

    async def load()       # Load registry from Redis
    async def save()       # Persist to Redis (only if dirty)
    def sanitize(text)     # Redact PII using case-scoped registry
    def reverse(text)      # Replace placeholders with originals
    async def cleanup()    # Delete Redis key (case closed)
```

**Key behaviors:**

- `sanitize()` delegates PII detection to `DataSanitizer.sanitize_text_with_registry()` but passes the case-scoped `_forward` registry so the same value always gets the same placeholder
- `reverse()` sorts placeholders by length descending to avoid partial replacement (`<IP_ADDRESS_10>` before `<IP_ADDRESS_1>`)
- Redis key: `redaction:{case_id}`, value: JSON `{"forward": {...}, "reverse": {...}}`, TTL: configurable (default 7 days)
- If Redis is unavailable: works in-memory (consistent within turn, not across turns)
- If `enabled=False`: both methods return input unchanged (zero overhead)

### DataSanitizer.sanitize_text_with_registry()

**File:** `infrastructure/security/redaction.py`

A new method on `DataSanitizer` identical to `_sanitize_text()` but accepting an external `entity_registry` instead of creating a fresh one. This is the integration point between case-scoped redaction and the existing PII detection pipeline (regex patterns + Presidio NLP).

The existing `sanitize()` and `_sanitize_text()` methods are unchanged.

### DataSanitizer Detection Pipeline

The sanitizer has two detection stages, both configurable through `ProtectionSettings`:

**Stage 1 — Regex patterns** (`pattern_replacements`): Matches secrets and credentials (API keys, AWS keys, database URLs, JWT tokens, passwords). Password patterns use `\b` word boundaries to avoid corrupting compound tokens in log data (e.g., `failed_password: 520` must survive intact).

**Stage 2 — Presidio NLP** (`_apply_presidio()`): Sends text to Presidio Analyzer/Anonymizer services for NLP-based entity detection. Two settings control behavior:

- `entities_to_protect` — which entity types Presidio detects. Default excludes `PERSON`, `DATE_TIME`, `NRP`, `LOCATION`, and `URL` because spaCy NER (trained on prose) produces false positives on machine-generated log data (month abbreviations, hostnames, and syslog fields misclassified as person names).
- `min_score_threshold` — minimum confidence for Presidio detections (default: 0.85). Higher thresholds reduce false positives at the cost of missing some true PII.

Both values are read from `ProtectionSettings` in the constructor. The constructor always resolves settings via `get_settings()` if none are injected.

### MilestoneEngine Integration

**File:** `core/investigation/milestone_engine.py`

The engine manages the redaction lifecycle within `_process_turn_impl()`:

1. **Create context** — after case loading, before prompt generation
2. **Redact prompt** — at the entry to `_generate_structured_output()`, covering both DA (tool-augmented) and single-shot paths
3. **Redact tool results** — in `_tool_augmented_generate()` after `_format_tool_result()` and before truncation/append
4. **Save registry** — after LLM call completes, before returning result
5. **Return context** — included in the result dict so `InvestigationService` can reverse-substitute

The `_should_redact()` helper checks `SANITIZE_PII` setting. When `False`, `CaseRedactionContext` is created with `enabled=False` and all operations are no-ops.

### InvestigationService Integration

**File:** `modules/agent/domain/services/investigation_service.py`

The service manages two integration points:

**1. Extraction-layer redaction** — before the engine runs, during attachment preprocessing:

```python
redaction_context = await self._create_redaction_context(case.case_id)
for attachment in payload.attachments:
    evidence = await self._preprocess_attachment(
        case, attachment, ..., redaction_context=redaction_context,
    )
await redaction_context.save()  # Persist to Redis for engine
```

`_create_redaction_context()` loads the context from Redis (picks up any mappings from prior turns), and `classify_and_extract()` uses it instead of `DataSanitizer.sanitize()`. After all attachments are processed, the registry is saved so the engine picks up extraction-layer mappings.

**2. Response reverse-substitution** — after the engine returns:

```python
redaction_ctx = result.get("redaction_ctx")
if redaction_ctx:
    agent_response_text = redaction_ctx.reverse(agent_response_text)
```

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

`MilestoneEngine` has per-case asyncio locks (`_case_locks`). Registry load/save happens under the lock. No race conditions.

### Single Registry Path

When `SANITIZE_PII=true`, all redaction (extraction and inference) flows through the same `CaseRedactionContext`. There is no "double redaction" — the extraction layer's `classify_and_extract()` receives the context, and the engine loads the same context from Redis. When `SANITIZE_PII=false`, no redaction occurs at any layer.

### Placeholder in User Message

If a user types `<IP_ADDRESS_1>` in their message, `reverse()` would replace it with the original value. This is the correct behavior — the user is referencing a previously-seen entity.

## Files Changed

| File | Change | Risk |
| --- | --- | --- |
| `infrastructure/security/case_redaction.py` | New file | None |
| `infrastructure/security/redaction.py` | Added `sanitize_text_with_registry()`, wired Presidio config to settings, `\b` word boundary on password regex, removed dead code | Low |
| `core/investigation/milestone_engine.py` | Redaction lifecycle in turn processing + tool loop | Medium |
| `modules/agent/domain/services/investigation_service.py` | Extraction-layer context creation + reverse-substitution | Low |
| `modules/preprocessing/preprocessing_service.py` | `redaction_context` param on all 3 sanitize paths | Low |
| `container/providers/services.py` | Pass sanitizer + redis_client to engine | Low |
| `config/settings.py` | Added `redaction_registry_ttl_hours`, updated `entities_to_protect` defaults (removed false-positive-prone entities), raised `min_score_threshold` to 0.85 | Low |

## What Is Not Changed

- **Context builder** — no changes. Assembles raw content; redaction happens downstream
- **Router** — existing `_sanitize_if_needed()` stays as a safety net
- **Tool implementations** — `search_file`/`deep_analysis` are unchanged. Their raw results are redacted by the engine
- **Evidence storage** — evidence is stored raw, never redacted at rest

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
- `TestPresidioSettingsWiring` (4 tests): verifies settings injection, custom entity list honored, default entity list excludes `PERSON`/`DATE_TIME`/`NRP`/`LOCATION`/`URL`, default threshold is 0.85

### Integration Verification

Playbook scenario S6 (Cross-Evidence Correlation) with `SANITIZE_PII=true`:

- `failed_password: 520` survives intact in structural index (no `<PASSWORD_1>` corruption)
- No `<PERSON_N>` false positives on timestamps or log tokens
- IPs correctly redacted to `<IP_ADDRESS_N>` placeholders
- User-facing response shows real IPs (reverse-substituted)
- IPs from Linux_2k.log and OpenSSH_2k.log get different placeholders
- Response quality at parity with `SANITIZE_PII=false` runs
