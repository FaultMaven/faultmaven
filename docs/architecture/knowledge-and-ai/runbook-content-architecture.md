# Runbook Content Architecture: Structuring Knowledge for AI-Driven Troubleshooting

**Document Type:** Component Specification
**Version:** 3.0
**Status:** Design — v3 redesign (see [Implementation Status](#implementation-status))

## Purpose

This document answers a specific question: **How should the knowledge base be structured — its classification scheme, document templates, quality gates, and lifecycle rules — so that every runbook added to FaultMaven is well-organized, consistently structured, and reliably retrievable?**

### Where Knowledge Base Fits in the Investigation

FaultMaven's investigation engine consumes three types of data, all delivered to the LLM via RAG but fundamentally different in nature:

| # | Data Type | Lifecycle | Scope | Role in Investigation | Preprocessing |
|---|-----------|-----------|-------|----------------------|---------------|
| 1 | **User queries** | Ephemeral (per-turn) | Case-specific | Drives investigation direction | None (direct input) |
| 2 | **Submitted evidence** | Short-term (per-case) | Case-specific | Diagnose the problem — logs, metrics, configs from the user's environment | Data preprocessing pipeline (classification, extraction, structural indexing — see [data-preprocessing-design-specification.md](../data-processing/data-preprocessing-design-specification.md)) |
| 3 | **Knowledge base** | Long-term (permanent) | Cross-case | Remediate the problem — runbooks, procedures, known solutions | **This document** — content architecture, quality gates, lifecycle |

Type 2 (submitted evidence) and Type 3 (knowledge base) are both fed to the LLM through RAG, but they serve different purposes in the investigation:

- **Evidence** tells the AI *what is happening* — it is raw, case-specific, and diagnostic. It answers "what broke?"
- **Knowledge base** tells the AI *what to do about it* — it is curated, cross-case, and remedial. It answers "how do we fix it?"

This document defines the framework for Type 3 only. It specifies how to structure the knowledge container so that curated remediation knowledge is reliably retrievable when the investigation engine needs it.

### Scope

FaultMaven is a delivery mechanism and a growing framework for troubleshooting knowledge. It is not itself a knowledge source. The quality of FaultMaven's output depends entirely on the quality of what goes into the knowledge base. This architecture defines the container — not the content — to ensure that container maintains a high quality bar as the KB grows.

**Related Documents:**

- [knowledge-base-architecture.md](./knowledge-base-architecture.md) — Storage systems, vector stores, KB-neutral tool design
- `faultmaven/modules/knowledge/domain/models/knowledge_item.py` — Domain model
- `faultmaven/modules/knowledge/domain/services/conversion_service.py` — Scan, verify, and ingestion workflow
- `faultmaven/core/knowledge/ingestion.py` — Chunking and embedding

---

## 1. Design Principles

Three principles drive every decision in this architecture:

**1. FaultMaven must act as the SME.** Users trust FaultMaven because the knowledge base contains SME-grade procedures. The content must represent what a subject matter expert would do — authored or validated by someone with that expertise. The person using FaultMaven during an incident does not need to be an SME; that is the entire value proposition.

**2. Retrieval quality depends on document structure.** FaultMaven uses semantic vector search (BGE-M3 embeddings, cosine similarity via HNSW). Documents are split into chunks before indexing. If a runbook is poorly scoped, has vague language, or buries actionable steps behind long preambles, the retrieval will return irrelevant or low-signal chunks. The structure defined here is optimized for how the AI retrieves and applies knowledge, not just for human readability.

**3. The framework must enforce quality at the gate, not after the fact.** Once a poorly-written runbook is in the KB, it silently degrades every investigation that retrieves it. Quality gates at ingestion time are cheaper than debugging bad AI responses later.

---

## 2. Classification & Taxonomy

### Why Classify

Classification serves two functions:

1. **Retrieval filtering** — Narrow vector search to relevant domains instead of searching the entire KB
2. **Scope discipline** — Prevent overlapping runbooks that cause the AI to retrieve conflicting guidance

### Taxonomy Schema

Every runbook declares its classification in YAML frontmatter. These fields are stored as ChromaDB metadata, enabling filtered vector search (semantic similarity within a domain, not across all documents).

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `id` | string | Yes | Unique identifier for the runbook |
| `title` | string | Yes | Human-readable title |
| `domain` | string | Yes | Engineering vertical: `database`, `networking`, `compute`, `application`, `security`, `storage`, `messaging` |
| `service` | string | Yes | Specific technology: `postgresql`, `kubernetes`, `redis`, `nginx`, `kafka` |
| `symptom_class` | list of strings | Yes | Failure modes addressed: `latency`, `oom`, `connection_refused`, `timeout`, `disk_full`, `crash_loop`, `auth_failure` |
| `severity` | enum | Yes | Impact level: `critical`, `high`, `medium`, `low`, `info` |
| `scope` | enum | Yes | KB tier: `global`, `team`, `personal` |
| `tags` | list of strings | No | Additional search terms (e.g., `aws`, `gcp`, `linux`) |
| `difficulty` | enum | No | `beginner`, `intermediate`, `advanced`, `expert` |
| `version` | string | Yes | Semantic version of the runbook content |
| `last_updated` | date | Yes | ISO 8601 date — drives staleness detection |
| `verified_by` | string | Yes | SME who authored or approved the content |
| `status` | enum | Yes | Lifecycle state: `draft`, `in-review`, `verified`, `stale`, `deprecated` |

**Example frontmatter:**

```yaml
---
id: pg-connection-pool-exhaustion
title: "PostgreSQL Connection Pool Exhaustion"
domain: database
service: postgresql
symptom_class: [latency, connection_refused]
severity: high
scope: global
tags: [pgbouncer, aws-rds]
difficulty: intermediate
version: "1.0.0"
last_updated: 2026-03-21
verified_by: sre_team
status: verified
---
```

### Taxonomy Design Rules

**One runbook = one failure mode.** Do not create "Everything about PostgreSQL" documents. Instead:

- "PostgreSQL: Connection Pool Exhaustion"
- "PostgreSQL: Replication Lag"
- "PostgreSQL: Disk Full on WAL Directory"

Atomic runbooks produce better retrieval because the entire document is relevant to the query, not just a buried section.

**Fixed vocabulary for `domain` and `symptom_class`.** Free-text values drift over time. Maintain a controlled vocabulary so that metadata filtering works consistently. The lists above are starting points — extend them deliberately, not ad hoc. For long-tail symptoms that don't fit the controlled vocabulary (e.g., `split_brain`, `clock_skew`, `certificate_expiry`, `cache_stampede`), use the `tags` field. Tags are free-text and indexed in ChromaDB metadata, providing an escape valve for specific failure modes without diluting the core vocabulary.

**`service` is the technology, not the team.** Teams change; technologies are stable identifiers. Tag by what the runbook diagnoses, not who owns it.

**Taxonomy fields drive hybrid search filtering.** The `domain`, `service`, `symptom_class`, and `severity` fields are stored in ChromaDB metadata at ingestion time and consumed at query time by the four-signal reranker (metadata-match signal) and by the optional hard pre-filter mode (`filter_mode="hard"` injects domain/service into the ChromaDB `where` clause). See [vector-retrieval-architecture.md §3](./vector-retrieval-architecture.md#3-two-stage-retrieval-and-reranking-pipeline) for the implementation.

---

## 3. Standardized Runbook Template

### Why Structure Matters for RAG

FaultMaven's ingestion pipeline uses **structure-aware chunking** — it splits runbooks at markdown header boundaries (`##`, `###`), not at fixed character counts. Each `##` section becomes its own chunk; each `### Cause N` subsection within `## Causes` becomes its own chunk. During retrieval, the agent sees individual chunks, not the full document.

Chunking parameters (implemented in `kb_toolkit/core/chunker.py`):

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Split strategy | Markdown header boundaries (`##`, `###`, `####`) | Each template section becomes a semantic unit |
| Max chunk size | 3000 characters | Oversized sections split at sentence boundaries |
| Min chunk size | 100 characters | Tiny sections merged with adjacent section |
| Fallback | Sentence-boundary splitting | For structureless text without headers |
| Frontmatter | Stripped before chunking | Metadata stored separately in ChromaDB, not embedded |
| HTML comments | Stripped before chunking | Predicate hints lifted to ChromaDB metadata (see [runbook-cause-matching.md §6](../investigation-engine/runbook-cause-matching.md#6-chromadb-metadata-schema)) |

This means:

- **Each Cause subsection is one chunk.** Every `### Cause N` becomes a single chunk containing the cause statement, mechanism, indicator, mitigation, resolution, and verification together. Retrieval surfaces the complete cause-fix tuple — not a fragment, not a multi-chunk reconstruction.
- **Section size matters.** Aim for 400-900 characters per Cause subsection. Subsections over 3000 chars get split at sentence boundaries, breaking field co-location. Subsections under 100 chars get merged with neighbors, losing their header context.
- **Each Cause subsection is self-contained** — a chunk that says "as described in Cause A" provides no value. The retrieved chunk may be the only chunk the agent sees for a given query.
- **Only actionable content in the runbook body** — authoring guidelines, rationale, and commentary belong in this architecture doc, not in the runbook itself. Every sentence in the runbook gets embedded; non-actionable text dilutes the embedding and wastes retrieval signal.

### The Template

This template structures each runbook around **per-Cause sections**. Each `### Cause N` subsection is a self-contained chunk carrying the cause statement, mechanism, indicator, and inline mitigation/resolution/verification. This design lets retrieval surface complete cause-fix tuples in a single chunk and lets case completion populate `RootCauseConclusion` + `Solution` by direct field copy without an LLM extraction call.

**Design rule:** The template below contains ONLY what should appear in the final runbook. Authoring guidance is in the [Authoring Rationale](#authoring-rationale) section below the template.

````markdown
---
# [YAML taxonomy frontmatter — see Section 2]
---

# Runbook: [Title — include the failure mode, not just the technology]

## Symptom Recognition
- Exact alert names: "Datadog Alert: PostgreSQL Connection Pool > 90%"
- Error messages as they appear in logs: "FATAL: too many connections for role"
- Metric patterns: "pg_stat_activity active connections > pool_size for >5min"

## Applicability
PostgreSQL 14+ (applies to AWS RDS, Aurora, self-hosted). Requires `pg_monitor` role or superuser access. Tools: `psql`, `pgbouncer` admin console.

## Diagnostic Steps

### Step 1: Check active connections
```sql
SELECT count(*), state FROM pg_stat_activity GROUP BY state;
```
Expected output: count per connection state.

### Step 2: Identify idle-in-transaction sessions
```sql
SELECT pid, now() - query_start AS duration, query
FROM pg_stat_activity WHERE state = 'idle in transaction'
ORDER BY duration DESC;
```
Expected output: list of idle-in-transaction sessions with age.

## Causes

### Cause A: Idle transactions exhausting the pool
**Statement:** Sessions in `idle in transaction` hold connection slots indefinitely, exhausting `max_connections` under steady churn.
**Mechanism:** Each idle-in-transaction session retains a connection slot until it commits, rolls back, or is forcibly terminated. With pooled clients that fail to release on application errors, slots accumulate faster than they are released, eventually reaching `max_connections` and blocking new connections.
**Indicator:**
- [Step 1] active connections > 80% of max_connections
- [Step 2] sessions with state = 'idle in transaction' older than 30 minutes present
<!-- match: {"step": 2, "predicate": "contains", "target": "idle in transaction"} -->
**Mitigation:**
- **Risk:** Forcibly terminating connections may roll back in-flight transactions on application clients.
- **Command:**
  ```sql
  SELECT pg_terminate_backend(pid) FROM pg_stat_activity
  WHERE state = 'idle in transaction' AND query_start < now() - interval '30 minutes';
  ```
- **Duration:** Safe for up to 24 hours while root cause is addressed.
**Resolution:**
```sql
ALTER SYSTEM SET idle_in_transaction_session_timeout = '30s';
SELECT pg_reload_conf();
```
**Verification:** Re-run Step 2; sessions older than the new timeout should not accumulate.

### Cause B: Connection pool undersized for current load
**Statement:** Allocated pool size is below the steady-state working set of concurrent connections.
**Mechanism:** Application connection demand exceeds the pool's `max_pool_size`, causing new requests to wait or fail. Unlike Cause A, no idle-in-transaction sessions accumulate; the pool is genuinely saturated by active work.
**Indicator:**
- [Step 1] active connections > 80% of max_connections
- [Step 2] no sessions in idle-in-transaction state
<!-- match: {"step": 1, "predicate": "threshold", "target": "active_pct", "op": ">", "value": 0.8} -->
**Mitigation:**
- **Risk:** Increasing pool size raises memory consumption on the database.
- **Command:**
  ```ini
  # pgbouncer.ini
  max_pool_size = 50  # increase from default 20
  ```
  ```bash
  pgbouncer -R  # reload config
  ```
- **Duration:** Permanent once memory headroom confirmed.
**Resolution:** Same as Mitigation.
**Verification:** Re-run Step 1; active connections should stabilize below 70% of the new pool size under normal load.

### Cause Z: Unidentified
**Statement:** None of the documented causes match the observed evidence.
**Mechanism:** The runbook's known failure patterns do not cover this case; root cause requires investigation outside the runbook's scope.
**Indicator:**
- [Default]
**Mitigation:**
- **Risk:** Generic mitigation may not address the underlying cause; collect more evidence before applying.
- **Command:** Capture full `pg_stat_activity` snapshot and PostgreSQL log tail; consult database SME.
- **Duration:** Diagnostic only.
**Resolution:** Out of runbook scope. Escalate.
**Verification:** N/A.

## Prevention
- Set `idle_in_transaction_session_timeout` in postgresql.conf (prevents idle connection buildup)
- Add monitoring alert: "pg_stat_activity active > 80% of max_connections for 5 min"
- Review connection pool sizing quarterly against peak traffic patterns

## Sources
- [PostgreSQL: Connection Handling](https://www.postgresql.org/docs/current/runtime-config-connection.html) — official docs on connection limits and timeouts
- [PgBouncer Configuration](https://www.pgbouncer.org/config.html) — pool sizing parameters
````

### Authoring Rationale

This section explains WHY each template section is structured the way it is. This is guidance for runbook authors and the generation pipeline — it does NOT appear in the runbook itself.

| Section | RAG Purpose | What makes it effective |
|---------|------------|------------------------|
| **Symptom Recognition** | First retrieval signal. The agent matches user-reported symptoms against this section via vector similarity. | **Symptom-only co-location.** Keep alerts, error messages, and metric patterns together as a tight block. Do not mix with applicability or mechanism. Generic descriptions ("database is slow") match too many runbooks; specificity wins retrieval. |
| **Applicability** | Confirms whether the runbook applies to the user's environment. Scope context — system version, required tools, access requirements. | Concrete versions and tool names. The agent surfaces applicability when proposing the runbook to the user; vague scope ("works on Postgres") leads to misapplication. |
| **Diagnostic Steps** | The agent proposes these commands to the user during DIAGNOSIS. Each step's finding feeds Indicator evaluation in the active Cause. | **Procedure only — no interpretation.** Command, expected output shape, nothing else. The interpretation of what each finding *means* lives in each Cause's `Indicator` field, not here. Splitting them prevents the same interpretation from appearing in two chunks (Diagnostic Step chunk AND Cause chunk), which would dilute retrieval signal. |
| **Causes → `### Cause N`** | Each Cause subsection is one chunk. Retrieval surfaces a complete cause-fix tuple. Engine reads `Statement` + `Mechanism` for `RootCauseConclusion`; reads `Mitigation` / `Resolution` for `Solution`. | Per-Cause inlining of all relevant fields (statement, mechanism, indicator, mitigation, resolution, verification) keeps the chunk self-contained. Hard char limits on `Statement` (≤300) and `Mechanism` (≤800) enforce conciseness — these fields are copied verbatim into engine state, not summarized. |
| **Causes → `Statement`** | Direct copy → `RootCauseConclusion.root_cause` at case completion. | Single declarative sentence stating the cause. Not a fix, not a symptom — the cause. ≤300 characters. |
| **Causes → `Mechanism`** | Direct copy → `RootCauseConclusion.mechanism`. | How the cause produces the symptom — the causal chain. ≤800 characters. |
| **Causes → `Indicator`** | Engine evaluates against case evidence to attribute the active Cause. | Bullet list referencing `[Step N]` findings or `[Symptom]` patterns. Each entry must contain at least one reference token. Use `[Default]` for the `Cause Z: Unidentified` fallback. Optional `<!-- match: ... -->` HTML comment provides a machine-readable predicate; see [runbook-cause-matching.md §3](../investigation-engine/runbook-cause-matching.md#3-predicate-vocabulary) for vocabulary. |
| **Causes → `Mitigation` / `Resolution`** | `Mitigation` = quick risk-tagged fix (supports mitigation-first investigation). `Resolution` = durable fix. | Each block contains command + risk + duration (Mitigation) or command + durable change (Resolution). When the two are identical, use `**Resolution:** Same as Mitigation.` in the generation prompt; the generator expands the duplication at render time so the on-disk runbook always carries both fields populated. |
| **Causes → `Verification`** | Cause-specific check that confirms THIS fix worked. Feeds the `solution_verified` confirmation prompt. | Specific to the Cause's fix, not generic. Per-Cause verification because "did the fix work?" is per-Cause; "is the symptom gone?" is the engine's terminal gate, not a runbook concern. |
| **`### Cause Z: Unidentified`** | Fallback when no other Cause's Indicator matches. Engine selects this Cause when Indicator evaluation returns zero matches. | Mandatory in every runbook. Indicator is `[Default]`. Mitigation describes a safe diagnostic/escalation path; Resolution is typically "Out of scope". |
| **Prevention** | Used in post-resolution recommendations and report generation. **Rarely retrieved during active investigation** — Prevention chunks don't match symptom queries. They become relevant after the problem is resolved, when the agent generates recommendations. | Configuration changes, monitoring alerts, capacity thresholds — concrete actions. |
| **Sources** | Provenance for the knowledge. Enables verification and updates. | URL + brief description of what was used from each source. |

### Template Compliance Rules

1. **All 6 H2 sections required.** `Symptom Recognition`, `Applicability`, `Diagnostic Steps`, `Causes`, `Prevention`, `Sources`. Missing or renamed sections fail ingestion as a hard error.
2. **`## Causes` must contain ≥1 real `### Cause <X>` subsection plus exactly one fallback Cause** whose Indicator includes the `[Default]` token. The fallback Cause is conventionally named `### Cause Z: Unidentified` (validator enforces this heading for consistency); engine-side fallback detection reads only the `[Default]` Indicator token, not the heading text. Validator hard error if the real Cause count is zero or if no Cause carries `[Default]`.
3. **Cause heading convention.** Real Causes use `### Cause <X>: <name>` where `<X>` is a single uppercase letter `A` through `Y`. `Z` is reserved for the fallback. Validator hard error on heading format violation.
4. **Each `### Cause <X>` must contain all 6 sub-fields:** `**Statement:**`, `**Mechanism:**`, `**Indicator:**`, `**Mitigation:**`, `**Resolution:**`, `**Verification:**`. Validator hard error on missing field.
5. **Hard character limits.** `Statement` ≤300 chars, `Mechanism` ≤800 chars. Validator hard error on overflow. Generation pipeline re-prompts the LLM on overflow.
6. **Indicator format.** Each `**Indicator:**` entry must contain at least one of `[Step N]` (N must resolve to an existing numbered Diagnostic Step), `[Symptom]` (free-form reference back to Symptom Recognition), or `[Default]` (reserved for the fallback Cause). Validator hard error on missing token.
7. **Match-hint comments are optional but must be strict JSON when present.** The body of any `<!-- match: ... -->` block must be `json.loads()`-parseable (quoted keys, double quotes, no trailing commas, no JSON5/YAML-flow syntax) and must use a predicate from the controlled vocabulary (see [runbook-cause-matching.md §3](../investigation-engine/runbook-cause-matching.md#3-predicate-vocabulary)). Validator hard error on malformed JSON or unregistered predicate.
8. **Section titles must match exactly.** The quality gate linter checks for these headers. Variant names (e.g., "Troubleshooting" instead of "Diagnostic Steps") will fail validation.
9. **Indicator overlap warning.** Validator soft warning if two Causes within the same runbook share identical Indicator sets — Indicators should typically be mutually exclusive within a runbook (multi-match policy in [runbook-cause-matching.md §4](../investigation-engine/runbook-cause-matching.md#4-multi-match-policy)).
10. **Code blocks expected in Mitigation/Resolution.** Validator issues a quality warning (not a hard error) when code blocks are absent from a Cause's Mitigation or Resolution, because some fixes are procedural rather than command-based (e.g., "escalate to vendor", "failover to secondary region").

---

## 4. Quality Gates

Documents must pass quality gates before entering the KB. A document that fails is **not indexed** — it is returned to the author with specific errors.

### Gate 1: Syntax Validation

Validates YAML frontmatter completeness and correctness.

**Checks:**

- All required taxonomy fields are present (`id`, `title`, `domain`, `service`, `symptom_class`, `severity`, `scope`, `version`, `last_updated`, `verified_by`, `status`)
- Optional fields validated if present: `tags`, `difficulty`
- `status` is one of: `draft`, `in-review`, `verified`, `stale`, `deprecated`
- `domain` and `symptom_class` values are from the controlled vocabulary
- `last_updated` is a valid ISO 8601 date

**Implementation:** Validated during the scan → verify workflow in `conversion_service.py`

### Gate 2: Structural Linting

Validates the markdown document contains required sections, subsections, and fields with actionable content.

**Hard errors (block ingestion):**

- All 6 required H2 headers must be present: `Symptom Recognition`, `Applicability`, `Diagnostic Steps`, `Causes`, `Prevention`, `Sources`
- `## Causes` must contain ≥1 real `### Cause <X>` subsection where `<X>` is `A`–`Y`
- `## Causes` must contain exactly one fallback Cause subsection whose Indicator includes `[Default]` (conventionally named `### Cause Z: Unidentified`)
- Each `### Cause <X>` must contain all 6 sub-fields: `**Statement:**`, `**Mechanism:**`, `**Indicator:**`, `**Mitigation:**`, `**Resolution:**`, `**Verification:**`
- `Statement` ≤300 chars, `Mechanism` ≤800 chars
- Each `**Indicator:**` entry must contain at least one of `[Step N]`, `[Symptom]`, or `[Default]`
- `[Step N]` references must resolve to existing numbered Diagnostic Steps
- Any `<!-- match: ... -->` HTML comment must parse as **strict JSON** (`json.loads()`-parseable; no JSON5/YAML-flow) and must use a predicate from the controlled vocabulary (see [runbook-cause-matching.md §3](../investigation-engine/runbook-cause-matching.md#3-predicate-vocabulary))
- No section or sub-field is empty

**Quality warnings (do not block, flagged for author review):**

- No fenced code blocks found in any Cause's Mitigation or Resolution (some fixes are procedural, e.g., "escalate to vendor")
- Two Causes within the runbook share identical Indicator sets (Indicator overlap — see [runbook-cause-matching.md §4](../investigation-engine/runbook-cause-matching.md#4-multi-match-policy))
- Content length below 500 characters
- No external references or links

**Implementation:** `RunbookValidator` rewritten for v3 schema in `kb_toolkit/core/validator.py`. Section and subsection presence checked via header regex; sub-field presence checked per-Cause via labelled-field regex; Indicator tokens validated against the Diagnostic Steps inventory; match-hint JSON parsed and predicate name checked against the registered vocabulary.

### Gate 3: Semantic Density Check (Planned)

An LLM-driven pre-ingestion check that rejects runbooks containing only architectural descriptions without actionable diagnostic or resolution procedures.

**Rationale:** A document that explains *how a system works* without specifying *what to do when it breaks* does not belong in the troubleshooting KB. It may be valuable documentation, but it is not a runbook.

**Status:** Not implemented. When implemented, this gate should use a fast/cheap LLM (classifier-tier, e.g., Groq) to evaluate whether the document contains concrete commands, queries, or procedural steps.

---

## 5. Lifecycle & Governance

Knowledge degrades over time. Commands change, dashboards are renamed, services are deprecated. The lifecycle rules ensure the AI operates on current, high-confidence knowledge.

### Lifecycle States

| State | Definition | KB Behavior |
|-------|------------|-------------|
| **DRAFT** | Created, not yet submitted for review. | **Not ingested.** Exists only in file storage. Invisible to the AI. |
| **IN-REVIEW** | Submitted for SME or team admin review (supports the Personal → Team promotion workflow). | **Not ingested.** Under review, not yet approved. |
| **VERIFIED** | SME-approved, tested against real scenarios, current. | Fully searchable. Standard confidence weighting in retrieval. |
| **STALE** | `last_updated` > 6 months ago. Content may be outdated. Auto-tagged by `kb-stale-check` or set manually. | Still searchable, but retrieval injects a staleness warning into the AI's context: *"This runbook was last verified on [date]. Commands and procedures may be outdated."* |
| **DEPRECATED** | Replaced by a newer runbook or no longer applicable. | **Purged from ChromaDB.** Retained in audit storage for historical reference only. |

### State Transitions

- **DRAFT → IN-REVIEW**: Author submits for review (e.g., promoting a personal runbook to team KB, or requesting SME review).
- **IN-REVIEW → VERIFIED**: Reviewer (SME or team admin) approves. Author sets `status: verified` and `verified_by` field. Triggers ingestion into ChromaDB on next `kb-ingest` run.
- **DRAFT → VERIFIED**: Direct path when author is the SME. Sets `status: verified` and `verified_by`. Common for Global KB authoring by platform admin.
- **VERIFIED → STALE**: Automatic. `kb-stale-check` compares `last_updated` against current date. Transition at 6-month threshold. Can also be set manually.
- **STALE → VERIFIED**: Author updates content, bumps `version`, sets new `last_updated`, sets `status: verified`. Re-ingested on next `kb-ingest` run.
- **VERIFIED/STALE → DEPRECATED**: Author sets `status: deprecated`. Triggers `collection.delete(ids=[runbook_id])` on next pipeline run.

### Governance Practices

- **Quarterly review cadence**: All STALE items appear in a review dashboard. Owners re-verify or deprecate.
- **Incident-driven updates**: After every incident where a runbook was retrieved, check if it was accurate. Update if not. This is how the Knowledge Flywheel works — resolved cases validate or correct existing runbooks.
- **Usage tracking**: `KnowledgeItem` tracks `view_count`, `helpful_count`, `not_helpful_count`. Runbooks with low helpfulness scores are candidates for review. Runbooks never retrieved may be poorly written or cover non-existent failure modes.

---

## 6. Authoring Guidelines

### What Makes a Runbook Authoritative

A runbook is authoritative when it represents what an SME would do. This does not require the author to have personally experienced every failure. Authoritative sources include:

- **Vendor documentation** — Official docs for the technology (e.g., PostgreSQL docs on connection management)
- **Established operational procedures** — Well-known diagnostic patterns (e.g., "disk full" recovery is well-understood; you don't need incident history to write it)
- **Community-validated procedures** — Patterns documented across the industry with proven track records
- **Incident postmortems** — Your organization's historical records. Especially valuable for system-specific failure modes that general knowledge doesn't cover

The first three sources are sufficient for common infrastructure failure modes. The fourth is necessary for failures specific to your architecture (e.g., "our payment service fails silently when Redis is down due to a misconfigured retry policy").

### Anti-Patterns

| Anti-Pattern | Why It Fails | Fix |
|--------------|-------------|-----|
| Generic advice ("check the logs") | The AI already knows this. Adds no value over the base model. | Specify which logs, what to look for, and what the findings mean |
| Architecture-only content | Describes how a system works but not what to do when it breaks. Fails the semantic density check. | Add concrete diagnostic steps and resolution procedures |
| Stale commands | Outdated CLI flags or deprecated dashboards erode trust. User follows the step, it fails, trust in FaultMaven drops. | Version the runbook, review on schedule |
| Mega-runbooks across symptoms | "Everything about Service X" — covers multiple unrelated symptom classes; only a small section matches any given query, but the whole document competes in retrieval. | Split into one runbook per symptom class. Multiple `### Cause N` subsections within a runbook are expected and correct; multiple symptom classes are not. |
| Copy-pasted vendor docs | Low signal density. Chunks contain boilerplate that dilutes the embedding. | Summarize the relevant parts, add your operational context |
| Missing per-Cause verification | A Cause without its own `**Verification:**` field gives the engine no fix-specific check; `solution_verified` falls back to generic prompts and the agent cannot confirm the right fix worked. | Every `### Cause N` must carry `**Verification:**` for its specific Mitigation/Resolution |
| Overlapping Indicators | Two Causes within one runbook whose Indicator sets cannot be distinguished from case evidence force the engine into the multi-match branch every time. | Author Indicators as mutually exclusive sets; lean on Diagnostic Steps whose findings differ between Causes |
| Indicator without step or symptom reference | An `**Indicator:**` entry that lacks `[Step N]` / `[Symptom]` / `[Default]` cannot be matched deterministically and offers no anchor for `case_evidence_qa` either. | Every Indicator entry must carry at least one reference token |

---

## Implementation Status

This section tracks what is implemented versus planned. v3 redesign requires regeneration of all existing runbooks via the KB toolkit — no migration shim, no backward-compatibility for v2-shaped runbooks.

**Current template in use (v3):** All 59 built-in runbooks have been regenerated to the v3 schema (`Symptom Recognition`, `Applicability`, `Diagnostic Steps`, `Causes`, `Prevention`, `Sources`). `kb_toolkit/core/validator.py` has been fully rewritten for v3: it enforces the 6 required H2 sections, per-Cause sub-fields with char limits, Indicator token validation with step-number resolution, and match-hint JSON parsing with predicate vocabulary checks. The FaultMaven API's `runbook_validator.py` (used by the document-to-runbook conversion pipeline) still enforces v2 section headers — updating it to v3 is the remaining gap.

| Feature | Status | Location |
| --- | --- | --- |
| YAML frontmatter parsing | Implemented | `conversion_service.py` scan workflow |
| v3 structural linting (6 H2s + Cause subsections + sub-fields) | **Implemented (toolkit)** | `kb_toolkit/core/validator.py` — `_validate_structure()`, `_validate_causes()`, `_validate_cause_subfields()` |
| Char-limit enforcement (Statement ≤300, Mechanism ≤800) | **Implemented (toolkit)** | `kb_toolkit/core/validator.py` — `_validate_cause_subfields()` |
| Indicator token validation (`[Step N]`, `[Symptom]`, `[Default]`) | **Implemented (toolkit)** | `kb_toolkit/core/validator.py` — `_validate_indicator_field()` with step-number cross-reference |
| Match-hint JSON parsing + predicate vocabulary check | **Implemented (toolkit)** | `kb_toolkit/core/validator.py` — `_validate_match_hints()`; chunker metadata lift still pending |
| Per-Cause metadata fields (`cause_statement`, `cause_mechanism`, `cause_indicators`, `match_predicates`, `cause_mitigation`, `cause_resolution`, `cause_verification`, `is_fallback_cause`) | **Pending** | `kb_toolkit/core/ingester.py` — see [runbook-cause-matching.md §6](../investigation-engine/runbook-cause-matching.md#6-chromadb-metadata-schema) |
| FaultMaven API `runbook_validator.py` v3 update | **Pending** | `modules/knowledge/domain/services/runbook_validator.py` — still enforces v2 `REQUIRED_SECTIONS` |
| Taxonomy fields stored in ChromaDB | Implemented | `domain`, `service`, `symptom_class`, `severity`, `scope`, `status`, `last_updated`, `tags`, `document_type` propagated per chunk |
| Domain/service hard pre-filter | Implemented | `filter_mode="hard"` injects `domain`/`service` into ChromaDB `where` clause |
| Verification-weighted retrieval | Implemented | Status bonuses in four-signal reranker: `verified` +0.40, `draft` -0.10, `deprecated` -0.30 |
| Staleness warning in retrieval context | Implemented | `UnifiedKBConfig.format_chunk_metadata()` injects age-based warnings; reranker freshness signal applies half-life decay |
| Staleness detection (6-month auto-transition) | **Implemented (toolkit only)** | `kb-stale-check` CLI scans `last_updated`. FaultMaven API has no background job for state transitions yet. |
| Semantic density check (LLM-driven) | **Not implemented** | Planned for Gate 3 |
| Usage tracking | Implemented | `KnowledgeItem.view_count`, `helpful_count`, `not_helpful_count` |
| Ingestion pipeline with draft tracking | Implemented | `conversion_service.py` scan → verify workflow (`conversion_drafts` table) |

### Implementation Priority

1. **Per-Cause metadata in ingester** — lift `cause_statement`, `cause_mechanism`, `cause_indicators`, `match_predicates` into ChromaDB chunk metadata at ingest time. Required before engine-side `AnswerFromKB.cause` field and the indicator-resolution path can land.
2. **FaultMaven API `runbook_validator.py` v3 update** — align `REQUIRED_SECTIONS` and sub-field checks with the v3 schema so the document-to-runbook conversion pipeline produces v3-compliant runbooks.
3. **Chunker match-hint stripping** — strip `<!-- match: ... -->` HTML comments before chunking and lift predicates to ChromaDB metadata.
4. **Semantic density check (Gate 3)** — Reject runbooks containing only architectural descriptions. Requires classifier-tier LLM integration.
5. **Staleness background job** — Compare `last_updated` against current date and auto-transition `verified` → `stale` at the 6-month threshold. Requires job scheduler integration.
