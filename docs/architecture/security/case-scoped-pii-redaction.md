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

**After:** `InvestigationService.process_turn()` calls `redaction_ctx.reverse()` on the agent response before returning it to the user, in its `_absorb_engine_result` phase (`investigation_service/turn_results.py`). The user sees original values.

## Architecture

Redaction is a **case-scoped, LLM-boundary concern** managed by the MilestoneEngine (which has `case_id`), not the Router (which is a generic LLM abstraction).

```text
User uploads file
    → stored raw (never redacted at rest)
                                            ↓
InvestigationService.process_turn()
  └─ Preprocessing (classify_and_extract) builds the file's summary and
     structural index from the raw content; both are persisted raw
                                            ↓
MilestoneEngine._process_turn_impl()
  ├─ Create CaseRedactionContext, load its registry from Redis   (_generate_turn_response)
  ├─ Context builder assembles the prompt from evidence, uploads, user message, history (raw)
  ├─ Redact the prompt with the case-scoped registry              (StructuredOutputGenerator)
  ├─ Send to LLM
  ├─ LLM calls tool → execute → redact result with SAME registry → return to LLM
  ├─ LLM responds with placeholders
  └─ Save registry to Redis                                       (_persist_turn)
                                            ↓
InvestigationService.process_turn()
  ├─ turn_results._absorb_engine_result: reverse-substitute placeholders → original values
  └─ turn_response._build_turn_response: return TurnResponse to user (real IPs, names, etc.)
```

Redaction happens at one boundary: where content leaves for an LLM. Everything upstream (the upload, the summary and structural index preprocessing builds from it, the evidence) is stored and assembled raw. The engine redacts the assembled prompt and every tool result with the case-scoped registry, so one consistent namespace covers all the PII that reaches the model, whichever file or turn it came from.

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

**Files:** `core/investigation/milestone_engine/engine.py` (the main turn path) and
`core/investigation/milestone_engine/terminal_turns.py` (`TerminalTurnHandler`,
the terminal Q&A path) — both build a `CaseRedactionContext` the same way,
reading `_should_redact()` from `core/investigation/milestone_engine/redaction.py`.
The `StructuredOutputGenerator` collaborator (`generation.py`) redacts the
prompt and tool results within the shape it owns.

The engine manages the redaction lifecycle across `_process_turn_impl()` and its
phases:

1. **Create context** — after case loading, before prompt generation, in `_generate_turn_response` (`turn_generation.py`)
2. **Redact prompt** — at the entry to `StructuredOutputGenerator.generate_structured_output()`, covering both DA (tool-augmented) and single-shot paths
3. **Redact tool results** — in `StructuredOutputGenerator._tool_augmented_generate()` after `_format_tool_result()` and before truncation/append
4. **Save registry** — after LLM call completes, before returning result, in `_persist_turn` (`turn_completion.py`)
5. **Return context** — threaded from `_generate_turn_response` through `_persist_turn` as a phase input/output, then included in the result dict so `InvestigationService` can reverse-substitute

The `_should_redact()` helper checks `SANITIZE_PII` setting. When `False`, `CaseRedactionContext` is created with `enabled=False` and all operations are no-ops.

### InvestigationService Integration

**File:** `modules/agent/domain/services/investigation_service/turn_results.py`

The service has one integration point, **response reverse-substitution**: after the engine returns, in `process_turn`'s `_absorb_engine_result` phase (`modules/agent/domain/services/investigation_service/turn_results.py`):

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

When `SANITIZE_PII=true`, all redaction flows through one `CaseRedactionContext` per case, applied at the LLM boundary only: the prompt and each tool result. There is no "double redaction". Preprocessing does not redact, and the Router's sanitizer sees only placeholders. When `SANITIZE_PII=false`, no redaction occurs at any layer.

### Not Implemented: Redaction During Preprocessing

Preprocessing does not redact. An upload's persisted summary and structural index hold raw content, as the upload itself does, and are redacted like any other prompt content when they reach an LLM. Redacting at extraction time, so that the persisted preprocessing artifacts carry placeholders rather than values, would be implemented if it is needed in the future. It would have to use the same case registry, or its placeholders would disagree with the ones the inference layer assigns.

### Placeholder in User Message

If a user types `<IP_ADDRESS_1>` in their message, `reverse()` would replace it with the original value. This is the correct behavior — the user is referencing a previously-seen entity.

## Where It Lives

| File | Role |
| --- | --- |
| `infrastructure/security/case_redaction.py` | `CaseRedactionContext`: the case-scoped registry, persisted in Redis |
| `infrastructure/security/redaction.py` | `DataSanitizer`, including `sanitize_text_with_registry()`, the Presidio settings wiring, and the `\b` word boundary on the password regex |
| `core/investigation/milestone_engine/turn_generation.py` | Creates the context and loads its registry (`_generate_turn_response`) |
| `core/investigation/milestone_engine/generation.py` | Redacts the prompt and each tool result (`StructuredOutputGenerator`) |
| `core/investigation/milestone_engine/turn_completion.py` | Saves the registry (`_persist_turn`) |
| `core/investigation/milestone_engine/terminal_turns.py` | The same lifecycle on the terminal Q&A path |
| `modules/agent/domain/services/investigation_service/turn_results.py` | Reverse-substitution (`_absorb_engine_result`) |
| `container/providers/services.py` | Passes the sanitizer and the Redis client to the engine |
| `config/settings.py` | `redaction_registry_ttl_hours`, `entities_to_protect`, `min_score_threshold` |

## What Redaction Does Not Touch

- **Preprocessing**: builds summaries and structural indexes from raw content (see *Not Implemented* above)
- **Context builder**: assembles raw content; the engine redacts downstream
- **Router**: its `_sanitize_if_needed()` stays as a safety net for LLM calls outside an investigation turn
- **Tool implementations**: `search_file` and `deep_analysis` read raw content, and the engine redacts their results
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
- `TestPresidioSettingsWiring` (4 tests): verifies settings injection, custom entity list honored, default entity list excludes `PERSON`/`DATE_TIME`/`NRP`/`LOCATION`/`URL`, default threshold is 0.85

### Integration Verification

Playbook scenario S6 (Cross-Evidence Correlation) with `SANITIZE_PII=true`:

- `failed_password: 520` survives intact where the structural index reaches the prompt (no `<PASSWORD_1>` corruption)
- No `<PERSON_N>` false positives on timestamps or log tokens
- IPs correctly redacted to `<IP_ADDRESS_N>` placeholders
- User-facing response shows real IPs (reverse-substituted)
- IPs from Linux_2k.log and OpenSSH_2k.log get different placeholders
- Response quality at parity with `SANITIZE_PII=false` runs
