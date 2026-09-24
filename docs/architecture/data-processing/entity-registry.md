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
| `mention_count` | **The number of distinct lines of this evidence that contain the entity** — one line = one mention. Uniform across every entity type, with one declared exception: `STRUCTURED_CONFIG` counts matches over the whole document. Aggregated across evidence by `list_top_entities`. See *The mention unit* below. |
| `in_error_context` | True when the entity appeared primarily in error/warning lines. Lets the agent distinguish *"IP X was involved in an error"* from *"IP X showed up in ambient traffic"*. Per-evidence, not global. |
| `first_seen_ts` | Populated from the evidence's `coverage_start_ts` (Phase 3a) when the evidence is time-bound, else NULL. Lets the registry answer temporal questions without re-opening the evidence. |

The composite primary key makes the write path **idempotent** — re-extracting an evidence (Phase 1.5 reclassification, Phase 2 retry) upserts by the full tuple rather than appending duplicates.

### The mention unit

`mention_count` is **the number of lines the entity appears on**, and it means
the same thing for every `entity_type` in every extractor over time-ordered
evidence — logs, command output, traces (fm#1587). A value named twice on one
line is **one** mention; the same value on two lines is two.

This is not a formatting detail. `list_top_entities` ranks with
`SUM(mention_count) DESC` and the top rows are auto-injected into the
investigation prompt, so the unit has to be comparable across types or the
ranking is meaningless. It was not, for one release: fm#1574 moved `user` to
per-line counting and left `ip`, `port`, `pid` and `path` counting regex
matches, so one column carried two units and a log format with a redundant IP
field could outrank a genuinely dominant username.

The consequence worth stating explicitly, because it was the argument against
the ruling and is settled: **an IP that is both source and destination on one
line is one mention.** A mention answers *"in how many log events did this
entity appear?"*, and one line is one event. Anyone who later needs
source-versus-destination back needs a **field**, not a count — overloading
`mention_count` to carry role information would reintroduce exactly the
divergence above.

#### The one exception: STRUCTURED_CONFIG counts matches

`ConfigEntityExtractor` counts **matches over the whole document**, via
`entities.line_tally.tally_document_matches` rather than `tally_entity_lines`.
This is a declared exception, not an oversight, and it is open: the scope
question is with the owner.

The per-line unit's justification is *a line is one event*, and **a config has
no events** — it is a structure. Flow-style YAML, minified JSON and single-line
`key=v key=v` blocks are ordinary config evidence and put a whole config on one
physical line. Counting lines there flattens every value to 1 and the ranking
degenerates. Measured on the same bytes:

```
per line       ONE physical line   db1.internal 1, 5432 1, pgbouncer 1, db2.internal 1, 6432 1   <- flat tie
               newline-separated   db1.internal 2, 5432 2, pgbouncer 1, db2.internal 1, 6432 1

per document   either form         db1.internal 2, 5432 2, pgbouncer 1, db2.internal 1, 6432 1
```

`SUM(mention_count) DESC` over the tie is insertion-ordered, so the five
entities reaching the prompt would be arbitrary — strictly worse than the match
count it would have replaced. Until the unit has an argument that reaches
declarative evidence, configs keep match counting, and that fact is here rather
than implicit. The guard pins the exception by name
(`test_every_fixture_declares_a_known_scope`), so a *second* data type going
document-scoped fails rather than passing quietly.

#### Also not per line

- The logs extractor's Windows Update KB, Windows CBS HRESULT and Apache
  mod_jk worker-state tallies are *occurrence* counts rendered as prose in the
  structural index; they render "occurrences", not "lines", and none of them
  reaches this table. A line carrying two HRESULTs recorded two events.
- The `IP auth breakdown` block's `auth total` is **not** a line count: it
  counts **attempts**, by outcome line (fm#1627). sshd logs one password
  attempt against an invalid user on three lines that carry the IP (`Invalid
  user`, the `pam_unix` authentication failure, `Failed password`), and
  exactly one of them — the outcome — is written once per attempt. sshd
  writes that outcome for every method, `Failed <method> for …` /
  `Accepted <method> for …` (password, publickey, keyboard-interactive/pam,
  hostbased, gssapi-*), so per IP the total is the number of lines carrying
  one, counted once per line. `Failed none` is excluded: it is the client's
  initial method query and carries no credential (loghub OpenSSH_2k has
  four). Where the IP has no outcome line, the total is the
  `pam_auth_failure` count, because Format B logs (loghub Linux) write none.
  The per-category numbers beside it are per line, are never added together
  (a `Failed password for invalid user` line matches two of them — fm#1596,
  which first stopped the summing and counted auth lines), and are what says
  *which* categories fired; outcomes for a method no category names are
  shown as `other_method_outcome=N`. Which IPs get a row is decided by any
  auth line or such an outcome, so an IP with only `Invalid user` lines
  renders `invalid_user=N → auth total=0` rather than vanishing — `0` means
  no authentication outcome and no PAM failure was logged for it.

#### Rows written before fm#1587

⚠️ `mention_count` values already in `case_entities` when fm#1587 lands carry
**match** counts for `ip`, `port`, `pid` and `path`. `list_top_entities` sums
across all of a case's evidence, so a long-lived case spanning the change mixes
match counts with line counts — the same "one column, two units" the section
above declares fixed, across *time* rather than across type. Nothing migrates
them: the counts are derived, and re-extracting the evidence is what corrects a
row. Treat pre-fm#1587 rows as approximate, or clear `case_entities` and
re-upload, until the affected cases are closed.

#### Where the rule lives

The unit is implemented once, at the point matches are produced, not at each
consumer:

- `extractors.utils.distinct_values` — the rule. **Every** producer goes
  through it, regex rules and non-regex matchers alike (`user` comes from
  `log_usernames.extract_usernames`), so a normalisation added here cannot
  reach some entity types and not others.
- `extractors.utils.distinct_on_line` — `distinct_values` over what a set of
  patterns matched on one line. Beside `split_log_lines`, which decides what a
  line *is*, and `is_port` / `is_pid` / `PID_MAX`, which both entity paths
  share so a raised ceiling cannot land on one of them only.
- `entities.line_tally.tally_entity_lines` — the one scanning loop, for the
  three per-line `EntityExtractor` implementations;
  `tally_document_matches` for the config exception.
- `LogsAndErrorsExtractor._build_entity_profile` takes the rule directly; it
  has fifteen other things to do per line and keeps its own loop.

Guard: `tests/unit/modules/preprocessing/test_mention_unit_is_one_line.py`. It
feeds every registered extractor a line naming each of its entity types twice
and checks the count against that type's *declared* scope; it fails if an
extractor is registered without such a line, if a fixture stops matching, if a
data type's scope changes, if the registry and the rendered profile disagree on
any entity type, or if a new loop anywhere in `entities/` iterates a raw
`findall` result.

## Entity type vocabulary

Initial set — defined in `faultmaven.modules.case.domain.models.EntityType`:

| Type | Captured by | Example | Notes |
| --- | --- | --- | --- |
| `ip` | logs, command_output, config, trace | `10.0.0.5`, `2001:db8::1` | Both IPv4 and IPv6. |
| `hostname` | config, trace | `db-master.prod.internal` | Not captured from logs — false-positive rate on syslog prefix is too high. |
| `user` | logs | `alice`, `root` | One rule, in `preprocessing/log_usernames.py`, shared with the logs extractor's entity profile (fm#522). `user=<name>` / `user <name>` on any line; `for [invalid user] <name>` only on a line carrying an SSH/PAM auth keyword — so `Failed password for root from …` **is** a hit (this row previously said it was not) while `installed for high-res timesource` is not. Reverse-DNS hostnames, PAM structural words and tokens that are the next field's name are rejected. Counted once per line, here and in the entity profile alike: the de-duplication is in `extract_usernames` itself, so there is one semantics from one place (fm#1574). Both branches capture the same token on `for invalid user <name>`, and counting each match ranked a scanner-sprayed account above a real one. The de-duplication key is case-folded within a line (both patterns are `IGNORECASE`, so `for Alice … user=alice` is one account) and the `user=` field's spelling is the one kept; across lines the spelling is still the entity identity. |
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

An implementation declares a table of `EntityRule` — which patterns (or non-regex matcher) find a type on a line, an optional validity test, whether it records `in_error_context` — and `entities.line_tally` does the scanning: `tally_entity_lines` for the three per-line extractors, `tally_document_matches` for the config exception above. The extractors own their vocabulary; they do not own the unit (fm#1587), and an extractor that hand-rolls its own `findall` loop fails the census in `tests/unit/modules/preprocessing/test_mention_unit_is_one_line.py`. A pattern with more than one capture group is refused when the rule is constructed, i.e. at import — the per-scan check would surface as "this evidence produced no entities at all", because `PreprocessingService` degrades any extraction exception to `[]`.

| Data type | Extractor | Entities emitted |
| --- | --- | --- |
| `LOGS_AND_ERRORS` | `LogsEntityExtractor` | `ip`, `user`, `port`, `pid`, `path` — with `in_error_context` derived from the logs extractor's severity scan. |
| `COMMAND_OUTPUT` | `CommandOutputEntityExtractor` | `ip`, `pid`, `port`, `path`. No error-context discrimination — command output doesn't have a stable severity signal. |
| `STRUCTURED_CONFIG` | `ConfigEntityExtractor` | `hostname`, `port`, `service`, `path`, `ip`. Key/value pairs only; the regex uses `[ \t]*` (not `\s*`) between key and value so nested YAML can't leak a keyword into the next key's value. **The one document-scoped extractor** — counts matches over the whole file, not lines; see *The mention unit*. |
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
