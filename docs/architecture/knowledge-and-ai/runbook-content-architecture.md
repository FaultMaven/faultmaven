# Runbook Content Architecture: Structuring Knowledge for AI-Driven Troubleshooting

**Document Type:** Component Specification
**Version:** 4.0
**Status:** Current — causal-chain template (see [Implementation Status](#implementation-status))

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
| HTML comments | Stripped before chunking | Comments never reach the embedding; per-Cause structure is lifted to metadata (see [kb-pack-architecture.md](./kb-pack-architecture.md)) |

This means:

- **Each Cause subsection is one chunk.** Every `### Cause N` becomes a single chunk carrying the cause statement, its causal chain, per-rung indicators, and the quadrant-tagged interventions together. Retrieval surfaces the complete cause→fix unit — not a fragment, not a multi-chunk reconstruction.
- **Section size matters.** Aim for 400-1200 characters per Cause subsection. Subsections over 3000 chars get split at sentence boundaries, breaking field co-location. Subsections under 100 chars get merged with neighbors, losing their header context.
- **Each Cause subsection is self-contained** — a chunk that says "as described in Cause A" provides no value. The retrieved chunk may be the only chunk the agent sees for a given query.
- **Only actionable content in the runbook body** — authoring guidelines, rationale, and commentary belong in this architecture doc, not in the runbook itself. Every sentence in the runbook gets embedded; non-actionable text dilutes the embedding and wastes retrieval signal.

### The Template

This template structures each runbook around **per-Cause causal chains**. Each
`### Cause N` declares **exactly one ROOT cause** and the chain from that root
down to the problem `D`, with per-rung indicators and quadrant-tagged
interventions. This mirrors the engine's two-dimensional hypothesis model (a
hypothesis is a causal chain, not a sentence — see
[two-dimensional-hypothesis-methodology.md](../investigation-engine/two-dimensional-hypothesis-methodology.md)),
so a retrieved Cause maps onto the case's causal graph by direct field copy.

**One Cause = one ROOT (no AND-sets).** Separate Causes are *mutually-exclusive
alternatives* — the engine treats them as OR-ed candidates and demotes one when
its fix fails — so a failure that needs two co-necessary conditions is **not**
two roots. "No AND-sets" forbids *competing roots*, not conjunction itself;
express the conjunction by its kind:

- **Causally sequential** (one condition enables the other) → make them `Chain`
  rungs (`root → s1 → D`), so the chain topology carries the conjunction. Prefer
  this whenever an ordering exists. (The chain is a *prior, not a gate* — a
  runbook-suggested cause enters the case graph as a CANDIDATE, never VALIDATED
  without case evidence, and `cause_state` is engine-derived +
  counterfactual-backstopped: a failed fix demotes the cause. Express genuine
  shared downstream states within a single Cause's chain; do not rely on
  cross-cause convergence being reconstructed in the case graph.)
- **Genuinely parallel** (neither condition causes the other, both simply
  required) → fold the co-necessary condition into the single root `Statement`
  and give each fix its own quadrant-tagged intervention. **The `Statement` must
  name *both* folded conditions at symptom level** — a `Statement` that names
  only one condition invites confirming the cause on partial evidence, the exact
  false conclusion the fold exists to prevent.

The engine's `and_group` AND-machinery exists for conjunctions the *engine* forms
at runtime; runbooks do not pre-author them. This keeps each Cause one
validate/refute/demote unit and maps cleanly to the engine's single-root
`Hypothesis`.

Two `Statement` invariants are load-bearing and validator-enforced: **the
`Statement` must be symptom-level** (it describes *what the evidence would show*
— the observable failure and its mechanism — not what an operator would run or
an internal API field), and **sibling cause `Statement`s must be mutually
discriminative (MECE, with teeth)** — each sibling distinguishable from the
others from case evidence alone.

Rejected alternative: deterministic `<!-- match -->` predicates feeding a
runbook-cause-matcher — retired without adoption (NO-GO 2026-07-08, #658);
runbooks serve as RAG context, and cause structure is a prior for the LLM, not a
gate.

**`Chain` is optional.** Omit it and the Cause is a degenerate `root → D` chain —
ingestion stays tolerant (no runbook breaks for lacking a decomposed ladder).

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
**Chain:**
- root: idle-in-transaction sessions never release their connection slot
- s1: free connection slots accumulate toward zero
- s2: `max_connections` reached; new connections are refused
- D: clients fail with "too many connections"
**Indicators:**
- root: [Step 2] sessions with state = 'idle in transaction' older than 30 minutes present
- s1: [Step 1] active connections > 80% of max_connections
**Interventions:**
- **remediation** (root): bound idle-in-transaction lifetime so slots are reclaimed.
  ```sql
  ALTER SYSTEM SET idle_in_transaction_session_timeout = '30s';
  SELECT pg_reload_conf();
  ```
  **Verification:** re-run Step 2; sessions older than the new timeout no longer accumulate.
- **mitigation** (s1): terminate the oldest idle-in-transaction sessions to free slots now.
  ```sql
  SELECT pg_terminate_backend(pid) FROM pg_stat_activity
  WHERE state = 'idle in transaction' AND query_start < now() - interval '30 minutes';
  ```
  **Risk:** may roll back in-flight client transactions. **Duration:** safe up to 24h. **Verification:** re-run Step 1; active connections drop.

### Cause B: Connection pool undersized for current load
**Statement:** `max_connections` is reached while the held slots are *genuinely active* queries (no idle-in-transaction sessions), so the pool is saturated by real working-set demand that exceeds its configured size.
**Indicators:**
- root: [Step 1] active connections > 80% of max_connections, and [Step 2] no idle-in-transaction sessions
**Interventions:**
- **remediation** (root): raise the pool size once DB memory headroom is confirmed.
  ```ini
  # pgbouncer.ini
  max_pool_size = 50  # increase from default 20
  ```
  ```bash
  pgbouncer -R  # reload config
  ```
  **Verification:** re-run Step 1; active connections stabilize below 70% of the new pool size under normal load.

### Cause Z: Unidentified
**Statement:** None of the documented causes match the observed evidence.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full `pg_stat_activity` snapshot and the PostgreSQL log tail; escalate to the database SME.
  **Risk:** diagnostic only. **Duration:** until SME review. **Verification:** N/A.

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
| **Diagnostic Steps** | The agent proposes these commands to the user during DIAGNOSIS; their findings become case evidence the agent reasons over. | **Procedure only — no interpretation.** Command, expected output shape, nothing else. What a finding *means* belongs in the cause `Statement`, with per-`Step` notes optionally mirrored in `Indicators`. Keeping interpretation out of the Steps chunk avoids duplicating it across the Diagnostic-Step and Cause chunks (which dilutes retrieval signal). |
| **Causes → `### Cause N`** | Each Cause subsection is one chunk and one **causal chain** terminating in a single ROOT. Retrieval surfaces a complete cause→fix unit. The root `Statement` seeds `RootCauseConclusion.root_cause`; `Interventions` seed `Solution` (`immediate_action`/`longterm_fix` by quadrant). | Per-Cause inlining of `Statement` / `Chain` / `Indicators` / `Interventions` keeps the chunk self-contained. One ROOT per Cause maps to the engine's single-root `Hypothesis`; no AND-sets are authored. |
| **Causes → `Statement`** | Direct copy → `RootCauseConclusion.root_cause`. **The cause's load-bearing one-line identity** — what the agent judges the case evidence against. | Single **symptom-level** declarative sentence — what the *evidence would show* (observable failure + mechanism), not a tool command or internal field. Must be discriminative from sibling Causes (MECE). Fold any *parallel* co-necessary condition in here; sequential co-necessity becomes `Chain` rungs instead. Not a fix, not a bare symptom. ≤300 characters. |
| **Causes → `Chain`** *(optional)* | Decomposes the causal ladder into rung nodes the engine instantiates as `CausalNode`s. Absence → degenerate `root → D` chain. | Linear `root → s1 → … → D`; each rung a short ref (`root`, `s1`, …, reserved `D`). No AND-gate — *sequential* co-necessity becomes rungs here; *parallel* co-necessity folds into the root `Statement`. Each rung ≤300 chars. |
| **Causes → `Indicators`** | Per-rung diagnostic notes — what observable finding ties each chain rung to a Diagnostic Step or reported symptom. They anchor the agent's evidence reasoning; the `Statement` remains the cause's identity. | Bullet list addressed by rung ref; each entry carries a `[Step N]` finding, `[Symptom]` pattern, or `[Default]` (fallback only). |
| **Causes → `Interventions`** | Each intervention seeds a `Solution` tagged with an `InterventionQuadrant` and the node it targets. | Per-node, quadrant-tagged: `remediation` (permanent @ root), `defensive_fix` (permanent @ intermediate), `mitigation` (temporary @ intermediate — carries **Risk** + **Duration**), `loop_break`. One root may carry two interventions (e.g. a `defensive_fix` and a `remediation`). Every intervention carries a `Verification` (feeds the `solution_verified` prompt). |
| **`### Cause Z: Unidentified`** | Fallback when none of the documented causes fit the observed evidence. | Mandatory in every runbook. Carries the `[Default]` Indicator token (the structural fallback marker — engine-side fallback detection reads it). Its single intervention is a `mitigation`/`loop_break` describing a safe diagnostic/escalation path. |
| **Prevention** | Used in post-resolution recommendations and report generation. **Rarely retrieved during active investigation** — Prevention chunks don't match symptom queries. They become relevant after the problem is resolved, when the agent generates recommendations. | Configuration changes, monitoring alerts, capacity thresholds — concrete actions. |
| **Sources** | Provenance for the knowledge. Enables verification and updates. | URL + brief description of what was used from each source. |

### Template Compliance Rules

1. **All 6 H2 sections required.** `Symptom Recognition`, `Applicability`, `Diagnostic Steps`, `Causes`, `Prevention`, `Sources`. Missing or renamed sections fail ingestion as a hard error.
2. **`## Causes` must contain ≥1 real `### Cause <X>` subsection plus exactly one fallback Cause** whose Indicator includes the `[Default]` token. The fallback Cause is conventionally named `### Cause Z: Unidentified` (validator enforces this heading for consistency); engine-side fallback detection reads only the `[Default]` Indicator token, not the heading text. Validator hard error if the real Cause count is zero or if no Cause carries `[Default]`.
3. **Cause heading convention.** Real Causes use `### Cause <X>: <name>` where `<X>` is a single uppercase letter `A` through `Y`. `Z` is reserved for the fallback. Validator hard error on heading format violation.
4. **Each `### Cause <X>` must contain the required sub-fields** `**Statement:**`, `**Indicators:**`, `**Interventions:**`; `**Chain:**` is optional. Validator hard error on a missing required field. (One ROOT per Cause; no AND-sets — sequence ordered co-necessity as `Chain` rungs, fold parallel co-necessity into the root `Statement`.)
5. **Hard character limits.** `Statement` ≤300 chars; each `Chain` rung ≤300 chars (soft warning). Validator hard error on `Statement` overflow; generation pipeline re-prompts on overflow.
6. **Indicator format.** Each `**Indicators:**` entry carries a rung ref and must contain at least one of `[Step N]` (N must resolve to an existing numbered Diagnostic Step), `[Symptom]` (free-form reference back to Symptom Recognition), or `[Default]` (reserved for the fallback Cause). Validator hard error on missing token.
7. **Interventions are quadrant-tagged.** Each `**Interventions:**` entry must lead with a valid quadrant — `remediation`, `defensive_fix`, `mitigation`, or `loop_break`. Validator hard error on a missing or unknown quadrant. Soft warnings: a `mitigation` should declare **Risk** + **Duration**; interventions should carry a **Verification**; command-based fixes should include a fenced code block.
8. **Section titles must match exactly.** The quality gate linter checks for these headers. Variant names (e.g., "Troubleshooting" instead of "Diagnostic Steps") will fail validation.
9. **`Chain` is optional and tolerant.** Omitting `Chain` yields a degenerate `root → D` chain (no error). When present it must be a linear `<ref>:` ladder; `converges: <Cause>.<ref>` is the only cross-chain construct. There is **no** AND grammar in authored runbooks.

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
- `## Causes` must contain exactly one fallback Cause subsection whose Indicators include `[Default]` (conventionally named `### Cause Z: Unidentified`)
- Each `### Cause <X>` must contain the required sub-fields `**Statement:**`, `**Indicators:**`, `**Interventions:**` (`**Chain:**` optional)
- `Statement` ≤300 chars (each `Chain` rung ≤300 chars — soft warning)
- Each `**Indicators:**` entry must carry a rung ref and at least one of `[Step N]`, `[Symptom]`, or `[Default]`
- `[Step N]` references must resolve to existing numbered Diagnostic Steps
- Each `**Interventions:**` entry must carry a valid quadrant (`remediation` / `defensive_fix` / `mitigation` / `loop_break`)
- No required section or sub-field is empty

**Quality warnings (do not block, flagged for author review):**

- A `mitigation` intervention without **Risk** + **Duration**
- A Cause's `Interventions` with no **Verification**, or no fenced code block when the fix is command-based (some fixes are procedural, e.g., "escalate to vendor")
- A `Chain` rung over 300 chars, or a `Chain` missing a `root:` / `D:` rung
- Content length below 500 characters; no external references or links

**Implementation:** `RunbookValidator` rewritten for the v4 causal-chain schema in `kb_toolkit/core/validator.py`. Section and subsection presence checked via header regex; required sub-fields (`Statement`/`Indicators`/`Interventions`, optional `Chain`) checked per-Cause via labelled-field regex; `Chain` validated tolerantly (absence = degenerate); Indicator tokens validated against the Diagnostic Steps inventory; intervention quadrants checked against the controlled set.

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
| Missing per-intervention verification | An intervention without a `**Verification:**` gives the engine no fix-specific check; `solution_verified` falls back to generic prompts and the agent cannot confirm the right fix worked. | Every intervention under `**Interventions:**` carries its own `**Verification:**` |
| Two roots / AND-sets in one Cause | A Cause with two roots (or an authored AND-gate) reads as duplicate nodes and does not map to the engine's single-root `Hypothesis`. | One Cause = one ROOT. Sequence co-necessary conditions as `Chain` rungs where an ordering exists; otherwise fold the *parallel* condition into the root `Statement` (which must **name both** conditions, so the cause is confirmed only when both are evidenced) and give each fix its own quadrant-tagged intervention (a `defensive_fix` *and* a `remediation` if needed). |
| Overlapping cause `Statement`s | Two sibling Causes whose symptom-level `Statement`s are not distinguishable from case evidence leave the agent unable to discriminate between them — both stay plausible and neither can be confirmed. | Author sibling `Statement`s as mutually discriminative (MECE with teeth); make the observable difference explicit in each `Statement`. |
| Tool-output-phrased `Statement` | A `Statement` written at operator/API level (`operationState.phase is Failed`) won't line up with symptom-level case evidence, so the Cause silently under-fires. | Phrase the `Statement` at symptom level — what the evidence shows (errors, log lines, exit codes) and the mechanism. |
| Indicator without step or symptom reference | An `**Indicators:**` entry that lacks `[Step N]` / `[Symptom]` / `[Default]` offers no anchor tying the rung to observable evidence. | Every Indicators entry carries a rung ref and at least one reference token |

---

## Implementation Status

This section tracks what is implemented versus planned. The v4 causal-chain
template is a **clean break** from v3 — no migration shim, no backward-compat for
v3-shaped runbooks (the validator rejects them).

**Toolkit (`faultmaven-kb-toolkit`) is on v4.** `validator.py`, `config.py`,
`quality.py`, `kb_init.py`, the `kb-researcher` author prompt, and `chunker.py`
all enforce/produce the v4 schema (`Statement` / optional `Chain` / `Indicators`
/ quadrant-tagged `Interventions`, one ROOT per Cause). The chunker strips
HTML comments and lifts per-Cause metadata.

**Content migration is complete.** All **91 built-in runbooks** (the 59
pre-existing + the 32 backlog) are authored in v4, and the vendored KB pack ships
all 91 with their per-Cause `causes` record. The FaultMaven API's
`runbook_validator.py` is on the v4 causal-chain schema and enforces the
cause-`Statement` invariants (symptom-level phrasing + sibling MECE, #545/#557).

| Feature | Status | Location |
| --- | --- | --- |
| v4 structural linting (6 H2s + `Statement`/`Chain`/`Indicators`/`Interventions`) | **Implemented (toolkit)** | `kb_toolkit/core/validator.py` — `_validate_causes()`, `_validate_cause_subfields()`, `_validate_chain()`, `_validate_interventions()` |
| Char-limit enforcement (`Statement` ≤300; `Chain` rung ≤300 soft) | **Implemented (toolkit)** | `kb_toolkit/core/validator.py` |
| Indicator token validation (`[Step N]`, `[Symptom]`, `[Default]`) | **Implemented (toolkit)** | `_validate_indicator_field()` with step-number cross-reference |
| Intervention quadrant validation | **Implemented (toolkit)** | `_validate_interventions()` against `valid_quadrants` |
| HTML-comment stripping + per-Cause metadata at chunk time | **Implemented (toolkit)** | `kb_toolkit/core/chunker.py` — `_post_process_chunk()` lifts `cause_*` + `is_fallback_cause` |
| Per-Cause metadata carried into KB pack | **Implemented** | `kb_toolkit/core/pack_builder.py` — `_extract_causes` writes the per-Cause graph record into `pack.json` `runbooks[].causes`; see [kb-pack-architecture.md](./kb-pack-architecture.md) |
| FaultMaven API `runbook_validator.py` v4 update | **Implemented** | `modules/knowledge/domain/services/runbook_validator.py` — v4 causal-chain schema + cause-`Statement` match-surface invariants (#545/#557) |
| Regenerate the 59 built-in runbooks to v4 | **Implemented** | all 91 built-ins (59 + 32) are v4; the vendored pack ships 91/91 with `causes` |
| Taxonomy fields stored in ChromaDB | Implemented | `domain`, `service`, `symptom_class`, `severity`, `scope`, `status`, `last_updated`, `tags` propagated per chunk |
| Domain/service hard pre-filter; verification-weighted + staleness-aware retrieval | Implemented | four-signal reranker; `UnifiedKBConfig` |
| Semantic density check (LLM-driven) | **Not implemented** | Planned for Gate 3 |

### Implementation Priority

1. **Regenerate the 59 built-in runbooks to v4** — the clean break means the KB cannot mix v3 + v4; the existing 59 must be brought to v4 before deploy alongside the 32 new ones.
2. **Per-Cause metadata into the KB pack** — carry the chunker's per-Cause metadata into `pack.json` so the structured cause record ships alongside the chunks.
3. **FaultMaven API `runbook_validator.py` v4 update** — align it with the v4 schema so the document-to-runbook conversion pipeline produces v4-compliant runbooks.
4. **Semantic density check (Gate 3)** — reject runbooks that are architectural description without actionable procedures. Requires classifier-tier LLM integration.
