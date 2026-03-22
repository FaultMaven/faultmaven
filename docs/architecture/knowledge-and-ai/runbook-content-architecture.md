# Runbook Content Architecture: Structuring Knowledge for AI-Driven Troubleshooting

**Document Type:** Component Specification
**Version:** 2.0
**Status:** Partial Implementation (see [Implementation Status](#implementation-status))

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
- `faultmaven/scripts/ingest_runbooks.py` — Ingestion pipeline and validator
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
| `scope` | enum | Yes | KB tier: `global`, `team`, `personal` |
| `tags` | list of strings | No | Additional search terms (e.g., `aws`, `gcp`, `linux`) |
| `difficulty` | enum | No | `beginner`, `intermediate`, `advanced` |
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
scope: global
tags: [pgbouncer, aws-rds]
difficulty: intermediate
version: "1.2"
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

**Fixed vocabulary for `domain` and `symptom_class`.** Free-text values drift over time. Maintain a controlled vocabulary so that metadata filtering works consistently. The lists above are starting points — extend them deliberately, not ad hoc.

**`service` is the technology, not the team.** Teams change; technologies are stable identifiers. Tag by what the runbook diagnoses, not who owns it.

---

## 3. Standardized Runbook Template

### Why Structure Matters for RAG

FaultMaven's ingestion pipeline splits documents into chunks (1000-character chunks with 200-character overlap, split on sentence boundaries). Each chunk is embedded independently. During retrieval, the AI sees individual chunks, not the full document.

This means:

- **Symptoms and related commands must be near each other** — if symptoms are in section 1 and the diagnostic command is in section 4, they land in different chunks and may not be co-retrieved
- **Each section must be self-contained enough to be useful in isolation** — a chunk that says "as described above" provides no value
- **Headers establish context windows** — the ChunkingService uses markdown headers to identify natural split points. Well-structured headers mean better chunk boundaries
- **Only actionable content should be in the runbook body** — authoring guidelines, rationale, and commentary belong in this architecture doc, not in the runbook itself. Every sentence in the runbook gets embedded; non-actionable text dilutes the embedding and wastes retrieval signal.

### The Template

This template mirrors FaultMaven's investigation stages (`SYMPTOM_VERIFICATION` → `HYPOTHESIS_FORMULATION` → `HYPOTHESIS_VALIDATION` → `SOLUTION`), allowing the AI to align runbook steps with the current case progress.

**Design rule:** The template below contains ONLY what should appear in the final runbook. Authoring guidance (the "Why" explanations) is in the [Authoring Rationale](#authoring-rationale) section below the template — it is for the author's reference, not for the runbook content.

```markdown
---
# [YAML taxonomy frontmatter — see Section 2]
---

# Runbook: [Title — include the failure mode, not just the technology]

## Context & Prerequisites (recommended, not required)
What system this applies to, required access permissions, and tools needed.

## Problem Definition
- Exact alert names: "Datadog Alert: PostgreSQL Connection Pool > 90%"
- Error messages as they appear in logs: "FATAL: too many connections for role"
- Metric patterns: "pg_stat_activity active connections > pool_size for >5min"

## Diagnostic Steps

### Step 1: Check active connections
```sql
SELECT count(*), state FROM pg_stat_activity GROUP BY state;
```
If active > 80% of max_connections, proceed to step 2.

### Step 2: Identify long-running queries
```sql
SELECT pid, now() - pg_stat_activity.query_start AS duration, query
FROM pg_stat_activity WHERE state = 'active' ORDER BY duration DESC;
```
Queries running >30s are candidates for termination.

## Mitigation
**Risk**: Forcibly terminating connections may kill active transactions.
```bash
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
WHERE state = 'idle in transaction' AND query_start < now() - interval '30 minutes';
```
**Verify**: Re-run Step 1. Active connections should drop below 80%.
**Duration**: Safe for up to 24 hours while root cause is addressed.

## Root Cause Resolution
**If** active connections dominated by idle transactions:
```bash
ALTER SYSTEM SET idle_in_transaction_session_timeout = '30s';
SELECT pg_reload_conf();
```

**If** connection pool undersized for current load:
```ini
# pgbouncer.ini
max_pool_size = 50  # increase from default 20
```
```bash
pgbouncer -R  # reload config
```

## Verification
- `pg_stat_activity` active connections stay below 70% of max_connections for 1 hour
- Application error rate returns to baseline (< 0.1%)
- No new "too many connections" errors in PostgreSQL logs

## Prevention
- Set `idle_in_transaction_session_timeout` in postgresql.conf (prevents idle connection buildup)
- Add monitoring alert: "pg_stat_activity active > 80% of max_connections for 5 min"
- Review connection pool sizing quarterly against peak traffic patterns

## Sources
- [PostgreSQL: Connection Handling](https://www.postgresql.org/docs/current/runtime-config-connection.html) — official docs on connection limits and timeouts
- [PgBouncer Configuration](https://www.pgbouncer.org/config.html) — pool sizing parameters
```

### Authoring Rationale

This section explains WHY each template section is structured the way it is. This is guidance for runbook authors — it does NOT appear in the runbook itself.

| Section | RAG Purpose | What makes it effective |
|---------|------------|------------------------|
| **Problem Definition** | The AI matches user-reported symptoms against this section via vector similarity. | Specific error strings and metric names produce precise retrieval. Generic descriptions ("database is slow") match too many runbooks. Put symptoms AND their associated error strings in the same paragraph so they land in the same chunk. |
| **Diagnostic Steps** | Used during HYPOTHESIS_VALIDATION. The AI proposes these commands to the user. | Each step must include: the command, what to look for in the output, and what the finding means. Vague steps ("check the database") force the AI to guess. |
| **Mitigation** | FaultMaven supports MITIGATION_FIRST investigation. The AI can propose a quick fix early. | Must include risk assessment, the command, verification, and safe duration. Without risk, the AI can't warn the user about side effects. |
| **Root Cause Resolution** | Linked to diagnostic findings. Structure as "If X, then Y" so the AI matches findings to fixes. | Each fix must be tied to a specific diagnostic outcome. Unlinked fixes force the AI to guess which one applies. |
| **Verification** | Lets the AI confirm whether the fix worked. | Specific metrics, commands, and observation periods. Without this, the investigation can't reach RESOLVED status. |
| **Prevention** | Used in post-resolution recommendations. | Configuration changes, monitoring alerts, capacity thresholds — concrete actions, not general advice. |
| **Sources** | Provides provenance for the knowledge. Enables verification and updates. | URL + brief description of what was used from each source. The AI can cite these when presenting the answer. |

### Template Compliance Rules

1. **Every section is required.** A runbook without diagnostic steps is a description, not a procedure. A runbook without verification is an unconfirmed guess.
2. **Code blocks are required in sections 2, 3, and 4.** A troubleshooting runbook without executable commands is not actionable.
3. **Section titles must match exactly.** The quality gate linter checks for these headers. Variant names (e.g., "Troubleshooting" instead of "Diagnostic Steps") will fail validation.

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

**Implementation:** `RunbookValidator.validate_runbook()` in `faultmaven/scripts/ingest_runbooks.py`

### Gate 2: Structural Linting

Validates the markdown document contains required sections with actionable content.

**Checks:**

- Required H2 headers are present: `Problem Definition`, `Diagnostic Steps`, `Mitigation`, `Root Cause Resolution`, `Verification`
- At least one fenced code block exists in `Diagnostic Steps` and `Root Cause Resolution` sections
- No section is empty (header with no content before the next header)

**Implementation:** `RunbookValidator.REQUIRED_SECTIONS` check in `faultmaven/scripts/ingest_runbooks.py`. Currently checks for: `Quick Reference Card`, `Diagnostic Steps`, `Solutions`, `Prevention`, `Related Issues`. These section names should be updated to match the canonical template defined in Section 3.

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
| Mega-runbooks | "Everything about Service X" — only a small section matches any given query, but the whole document competes in retrieval. | Split into one runbook per failure mode |
| Copy-pasted vendor docs | Low signal density. Chunks contain boilerplate that dilutes the embedding. | Summarize the relevant parts, add your operational context |
| Missing verification | A fix without a verification step is an unconfirmed guess. The AI can't tell the user whether the problem is actually resolved. | Always include "how to confirm the fix worked" |

---

## Implementation Status

This section tracks what is implemented versus planned.

| Feature | Status | Location |
|---------|--------|----------|
| YAML frontmatter parsing | Implemented | `ingest_runbooks.py:RunbookValidator` |
| Structural linting (required sections) | Implemented | `ingest_runbooks.py:REQUIRED_SECTIONS` |
| Taxonomy fields stored in ChromaDB | **Implemented (toolkit)** | KB Toolkit's `kb-ingest` propagates `domain`, `service`, `symptom_class`, `scope`, `status`, `last_updated` as distinct ChromaDB metadata keys. FaultMaven API ingestion pipeline (`ingest_runbooks.py`) not yet updated. |
| `domain`/`service`/`symptom_class` filtering | **Designed, not implemented** | Federated search with `where` clause filtering designed in [knowledge-base-architecture.md](./knowledge-base-architecture.md). Requires API-side implementation. |
| DRAFT/IN-REVIEW/VERIFIED/STALE/DEPRECATED lifecycle | **Partially implemented** | 5-state lifecycle defined. KB Toolkit enforces via `valid_statuses`. `KnowledgeItem` in FaultMaven API has `is_published` bool only — needs lifecycle enum. |
| Staleness detection (6-month auto-transition) | **Implemented (toolkit)** | `kb-stale-check` CLI scans `last_updated`, reports stale runbooks, `--auto-tag` updates frontmatter. FaultMaven API has no background job for this yet. |
| Staleness warning in retrieval context | **Designed, not implemented** | `KBConfig.format_chunk_metadata()` will inject warnings — see [knowledge-base-architecture.md](./knowledge-base-architecture.md) |
| Semantic density check (LLM-driven) | **Not implemented** | Planned for Gate 3 |
| Verification-weighted retrieval | **Not implemented** | `KnowledgeItem.verification_level` exists but is not used in search ranking |
| Usage tracking | Implemented | `KnowledgeItem.view_count`, `helpful_count`, `not_helpful_count` |
| Ingestion pipeline with change detection | Implemented | `ingest_runbooks.py:RunbookIngestionPipeline` (MD5 hash comparison) |

### Implementation Priority

1. **Store taxonomy in ChromaDB metadata** — Unblocks filtered search. Moderate effort: extend `_process_and_store()` in `ingestion.py` to propagate frontmatter fields.
2. **Lifecycle state enum on KnowledgeItem** — Replace `is_published` with a proper state field. Low effort, high value.
3. **Staleness background job** — Compare `last_updated` against current date. Requires job scheduler integration.
4. **Align validator section names** — Current `REQUIRED_SECTIONS` in `ingest_runbooks.py` don't match the canonical template. Quick fix.
5. **Verification-weighted retrieval** — Boost `ADMIN_VERIFIED` items in search results. Requires ChromaDB metadata filter or post-retrieval reranking.
