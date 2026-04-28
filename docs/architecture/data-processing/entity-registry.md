# Case-Level Entity Registry

**Version:** 1.0
**Date:** 2026-04-24
**Status:** Implemented (Phase 4a / 4b / 4c complete — ships dark behind `FAULTMAVEN_ENTITY_REGISTRY`)
**Context:** Design specification for the cross-artifact entity index introduced by the data-processing improvement plan's Phase 4.

---

## Problem

Before Phase 4, entity information (IPs, hostnames, users, PIDs, etc.) was:

1. **Logs-only.** Only `LogsAndErrorsExtractor._build_entity_profile` emitted entity data; other extractors surfaced nothing the agent could reason about.
2. **Embedded in prose.** Even for logs, the profile was a string wedged into the structural index — the agent had to re-parse it to use it.
3. **Per-evidence.** The profile summarised one file at a time. Answering *"where does IP 10.0.0.5 show up in this case?"* required the agent to LLM-scan every evidence summary.

Entity-driven reasoning ("which hosts are involved?", "did this IP appear before or after the outage?") is a first-class investigation pattern. Phase 4 makes it an indexed lookup instead of an LLM loop.

## Scope

- **In scope:** IPs, hostnames, usernames, PIDs, ports, services, paths, devices, metric names. Controlled vocabulary — extensions require a design-doc edit so the retrieval paths (agent tools, context-builder highlights) stay in sync with what producers emit.
- **Out of scope:** Entity *normalization* (e.g. merging `10.0.0.5` with `ip-10-0-0-5.ec2.internal`). Distinct string values remain distinct in the registry — a resolver layer is a separate design.
- **Not handled:** Cross-case aggregation. The registry is case-scoped by design; fleet-wide entity reasoning is a different problem (and a different privacy posture).

## Schema

Table: `case_entities`. Source: alembic migration `20260423_1400_d4e5f6a70819`.

```sql
CREATE TABLE case_entities (
    case_id          VARCHAR(36)  NOT NULL REFERENCES cases(case_id)    ON DELETE CASCADE,
    entity_type      VARCHAR(20)  NOT NULL,
    entity_value     VARCHAR(255) NOT NULL,
    evidence_id      VARCHAR(36)  NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
    mention_count    INTEGER      NOT NULL DEFAULT 1,
    in_error_context BOOLEAN      NOT NULL DEFAULT FALSE,
    first_seen_ts    TIMESTAMPTZ  NULL,
    PRIMARY KEY (case_id, entity_type, entity_value, evidence_id)
);
CREATE INDEX idx_case_entities_lookup     ON case_entities(case_id, entity_type, entity_value);
CREATE INDEX idx_case_entities_by_evidence ON case_entities(evidence_id);
```

**Column semantics:**

| Column | Semantics |
| --- | --- |
| `case_id`, `evidence_id` | Both cascade on delete. Case or evidence deletion sweeps registry rows without a separate cleanup job. |
| `entity_type` | Controlled vocabulary (see below). |
| `entity_value` | Raw string as extracted. Case-sensitive. Capped at 255 chars; anything longer is truncated before insert (lossless truncation is the extractor's job, not the registry's). |
| `mention_count` | How often the entity appeared in this specific evidence. Aggregated across evidence by `list_top_entities`. |
| `in_error_context` | True when the entity appeared primarily in error/warning lines. Lets the agent distinguish *"IP X was involved in an error"* from *"IP X showed up in ambient traffic"*. Per-evidence, not global. |
| `first_seen_ts` | Populated from the evidence's `coverage_start_ts` (Phase 3a) when the evidence is time-bound, else NULL. Lets the registry answer temporal questions without re-opening the evidence. |

The composite primary key makes the write path **idempotent** — re-extracting an evidence (Phase 1.5 reclassification, Phase 2 retry) upserts by the full tuple rather than appending duplicates.

## Entity type vocabulary

Initial set — defined in `faultmaven.modules.case.domain.models.EntityType`:

| Type | Captured by | Example | Notes |
| --- | --- | --- | --- |
| `ip` | logs, command_output, config, trace | `10.0.0.5`, `2001:db8::1` | Both IPv4 and IPv6. |
| `hostname` | config, trace | `db-master.prod.internal` | Not captured from logs — false-positive rate on syslog prefix is too high. |
| `user` | logs | `alice`, `root` | Only the explicit `user=…` / `for invalid user …` syslog forms. `for root from …` is intentionally not a hit. |
| `pid` | logs, command_output | `1234` | Bracket form (`sshd[1234]`), keyword (`pid=1234`), or column-aligned (ps/top output). Timestamp fragments like `04:47` are rejected. |
| `port` | logs, command_output, config | `22`, `5432` | Must have structural context — `port N`, `host:N`, `ip:N`. Bare numerics are not hits. |
| `service` | config, trace | `auth-api`, `checkout` | OTLP `service.name`, trace `peer.service`, config `service=` / `service_name=`. |
| `path` | logs, command_output, config, trace | `/var/log/app.log`, `/api/users/42` | HTTP route paths from logs, filesystem paths from command output (whitelisted `/var`, `/etc`, `/tmp`, `/home`, `/opt`, `/data` roots — `/usr/bin` is excluded as binary-path noise). |
| `device` | reserved | — | Not emitted by current extractors; vocabulary placeholder. |
| `metric_name` | reserved | — | Not emitted by current extractors; vocabulary placeholder. |

**Adding a type** requires:

1. A design-doc edit here describing the extractor contribution and the retrieval intent.
2. Adding the enum member to `EntityType`.
3. Teaching at least one `EntityExtractor` to emit it.
4. Deciding whether the context-builder highlights block should surface it (`_HIGHLIGHT_TYPES` in `prompts/context_builder.py`).

## Extractor contribution matrix

`EntityExtractor` is a `Protocol` in `faultmaven.modules.preprocessing.entities.protocol`. One implementation per data type; the dispatch table is `registry.extract_entities_for_data_type(data_type, content, error_line_indices)`.

| Data type | Extractor | Entities emitted |
| --- | --- | --- |
| `LOGS_AND_ERRORS` | `LogsEntityExtractor` | `ip`, `user`, `port`, `pid`, `path` — with `in_error_context` derived from the logs extractor's severity scan. |
| `COMMAND_OUTPUT` | `CommandOutputEntityExtractor` | `ip`, `pid`, `port`, `path`. No error-context discrimination — command output doesn't have a stable severity signal. |
| `STRUCTURED_CONFIG` | `ConfigEntityExtractor` | `hostname`, `port`, `service`, `path`, `ip`. Key/value pairs only; the regex uses `[ \t]*` (not `\s*`) between key and value so nested YAML can't leak a keyword into the next key's value. |
| `TRACE_DATA` | `TraceEntityExtractor` | `service`, `hostname`, `path`, `ip`. Handles both JSON (`"service.name":"x"`) and OTLP attribute (`service.name=x`) wire formats. `error=true` / `status.code: ERROR` trigger `in_error_context`. |
| `METRICS_AND_PERFORMANCE`, `UNSTRUCTURED_TEXT`, `SOURCE_CODE`, `VISUAL_EVIDENCE`, `UNANALYZABLE`, `DOCUMENTATION`, `ERROR_REPORT`, `PROFILING_DATA` | — | No registered extractor. `extract_entities_for_data_type` returns `[]`. |

Extractor failures are logged and degraded to an empty list — entity extraction is best-effort and must not block evidence persistence.

## Write path

Producer: `PreprocessingService._build_result` (for each extraction) + `InvestigationService._preprocess_attachment` (for persistence).

1. After `_build_result` assembles `PreprocessingResult`, if `entity_registry_enabled` is True and the result isn't a placeholder, call `extract_entities_for_data_type` against the raw content.
2. Bucket observations by `entity_type` and apply the per-(evidence, type) hard cap. The default cap is **500** rows per `(evidence, type)` pair, tunable via `FAULTMAVEN_ENTITY_REGISTRY_CAP`. Overflow is not dropped randomly — buckets are sorted by `mention_count` DESC before truncation so the retained rows are the most mentioned.
3. Each overflow event (one per `(evidence, type)` pair that overflowed) increments the `faultmaven_case_entities_overflow_total` counter labelled by `entity_type` and appends the type to `PreprocessingResult.entity_overflow_types`. The type list also lands on `evidence.metadata.entities.overflow_types` so the agent can see "the registry is incomplete for IP on this evidence."
4. `InvestigationService._preprocess_attachment` converts the observations to `CaseEntity` rows (clipping `entity_value` to 255 chars, enforcing `mention_count >= 1`, pulling `first_seen_ts` from Phase 3a's `coverage_start_ts`), and calls `CaseRepository.upsert_case_entities(case_id, evidence_id, entities)`.
5. `upsert_case_entities` is **replace-per-evidence**: it deletes all existing rows scoped to `(case_id, evidence_id)`, then inserts the new batch. An empty list clears without inserting — correct for timeless evidence or evidence whose re-extraction produced nothing.

Repositories that don't implement `upsert_case_entities` (legacy test doubles, partial mocks) are tolerated — the upload path does not fail.

## Read path

Two consumers: agent tools and the context-builder auto-injection.

### Agent tools

Registered in `container/providers/tools.py` gated on `FAULTMAVEN_ENTITY_REGISTRY`. When the flag is off neither tool appears in the LLM's function-calling menu, so the agent can't ask for registry data that isn't there.

- **`find_entity(entity_value, entity_type?)`** — `faultmaven.modules.agent.tools.find_entity_tool`. Exact-value lookup across the case's evidence; optional type filter; ordered by `mention_count` DESC. Returns one row per `(evidence, type)` the value appears in.
- **`list_top_entities(entity_type, limit=10)`** — `faultmaven.modules.agent.tools.list_top_entities_tool`. Aggregates `mention_count` across evidence and returns the top distinct values. `limit` clamped to `[1, 50]`.

Both delegate to `CaseRepository.find_entity` / `list_top_entities`.

### Context-builder highlights

`faultmaven.core.investigation.prompts.context_builder.fetch_entity_highlights` pre-fetches the top entities for four investigative-signal types (`ip`, `hostname`, `user`, `service`, top 5 each) and renders a compact `<entity_highlights>` block. Only populated types surface — a case with zero IPs but plenty of users produces a block with just a `user:` section.

The milestone engine calls `fetch_entity_highlights` before building the prompt when the feature flag is on and passes the result to `get_prompt_for_case` via the new `entity_highlights` kwarg. The `INVESTIGATION_BASE` template drops it into a slot between `{evidence}` and `{hypotheses}` — the LLM sees entity context right after the raw evidence that produced it. INQUIRY and TERMINAL templates don't reference the slot; `.format(**ctx)` tolerates extra keys.

Fetch failures are caught and degraded to an empty string — entity injection is best-effort and must not block a turn.

## Feature flags

| Flag | Default | Effect |
| --- | --- | --- |
| `FAULTMAVEN_ENTITY_REGISTRY` | `False` | Master switch. Off: no entity extraction, no registry writes, tools not registered, `fetch_entity_highlights` returns `""`. On: producer extracts + persists, tools appear to the LLM, highlights inject. |
| `FAULTMAVEN_ENTITY_REGISTRY_CAP` | `500` | Per-(evidence, type) hard cap. Clamped to `[1, 10000]`. |

The flag is designed so that turning it on or off is safe at any time: the `case_entities` table stays in schema, existing rows are preserved, and the feature flag simply gates *new* writes and *new* reads. Rows written during an on-period don't cause problems during an off-period — they're just orphaned until the flag is flipped back on.

## Observability

- **`faultmaven_case_entities_overflow_total{entity_type}`** — Counter. Incremented once per `(evidence, type)` overflow event, not per excess row. Exit-criteria dashboard reads the ratio of overflow/writes per type; the Phase 4 plan calls for tuning or splitting any type that overflows on >20% of evidence.
- Existing metrics that compose: `faultmaven_preprocessing_extraction_yield_ratio`, `faultmaven_evidence_dedup_hits_total`.

No metric is emitted on every successful entity write — row counts are queryable directly from `case_entities`.

## Rollback

1. Set `FAULTMAVEN_ENTITY_REGISTRY=false`. Producer skips writes, tools disappear, highlights go empty.
2. Existing rows stay in `case_entities`. They're inert — nothing reads from them.
3. The table itself is reversible via the alembic downgrade of `d4e5f6a70819` (drops two indexes + the table).

## Tests

- `tests/unit/modules/case/infrastructure/test_case_entities.py` — repository contract (24 cases across InMemory + SQLite).
- `tests/unit/modules/preprocessing/test_entity_extractors.py` — per-extractor false-positive/true-positive coverage, preprocessor integration, cap enforcement (27 cases).
- `tests/unit/modules/agent/test_preprocess_attachment_entities.py` — producer wiring through `_preprocess_attachment` (4 cases).
- `tests/unit/modules/agent/tools/test_find_entity_tool.py` + `test_list_top_entities_tool.py` — agent tool surface (18 cases).
- `tests/unit/core/investigation/test_entity_highlights.py` — `fetch_entity_highlights` and context-builder slot (12 cases).

All pass without Prometheus installed (the `_NoOpMetric` fallback in `evidence_metrics.py` handles that).

## Exit criteria

Per the Phase 4 plan — revisit monthly for the first quarter after default-on:

1. **Overflow hit rate.** If any `entity_type` hits overflow on >20% of evidence, tune the cap or split the type. Read from `faultmaven_case_entities_overflow_total / writes_for_type`.
2. **Agent adoption.** `find_entity` invoked ≥ 1×/investigation on cases with ≥ 2 evidence items. Check via `agent_tool_calls` rows where `tool_name = 'find_entity'`.
3. **Context-builder usefulness.** Auto-injection observed in at least one real case with ≥ 5 evidence items. Manually sample prompts captured by Opik to confirm.

Failing any of the three isn't a rollback signal — it's a tuning signal.

## References

- Case-schema table entry: [case-schema.md §4.13](../data-and-storage/schemas/case-schema.md).
- Migration: `alembic/versions/20260423_1400_d4e5f6a70819_phase_4_case_entities_registry.py`.
- Producer: `faultmaven/modules/preprocessing/entities/` + `preprocessing_service.py`.
- Consumer (tools): `faultmaven/modules/agent/tools/{find_entity_tool,list_top_entities_tool}.py`.
- Consumer (context): `faultmaven/core/investigation/prompts/context_builder.py:fetch_entity_highlights`.
