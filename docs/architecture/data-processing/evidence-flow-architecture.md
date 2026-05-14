# Evidence Flow Architecture

**Version:** 2.7
**Date:** 2026-04-26
**Status:** Live. The pipeline preprocesses attachments before the LLM, and Evidence is created during `INVESTIGATING` via the LLM's `evidence_to_add` (anchored to a `source_file_id` on `uploaded_files` or `source_type=user_description` for chat quotes). Categories are `symptom_evidence`, `causal_evidence`, `mitigation_evidence`, `solution_evidence`. The evidence taxonomy and DB schema live in [evidence-driven-investigation-framework.md §5](../investigation-engine/evidence-driven-investigation-framework.md#5-evidence-model) and [case-schema.md](../data-and-storage/schemas/case-schema.md) respectively.

---

## Overview

This document describes the complete evidence flow architecture in FaultMaven. All user turns arrive via a unified endpoint (`POST /cases/{id}/turns`) and are processed through a two-step pipeline: (1) preprocess attachments through Tier 0+1 before the LLM, (2) LLM inference with structural indexes in context. File attachments write only an `UploadedFile` row at intake (carrying preprocessing artifacts: `summary`, `structural_index`, `data_type`, coverage timestamps); Evidence rows are claim-anchored and born during `INVESTIGATING` when the LLM emits `evidence_to_add` referencing the source file via `source_file_id`. Source is expressed by `source_type` + `source_file_id` (the `evidence_source_invariant` DB CHECK requires one or the other). File preprocessing follows the [scenario-driven processing model](./data-preprocessing-design-specification.md). A mechanical query classifier routes each turn to Triage or Directed Analysis mode, which determines the system prompt and tool selection strategy. For DA-mode turns, vectorization is started proactively in the background for qualifying large files at the start of the tool loop; reactive fallback triggers remain for edge cases.

---

## System Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          FaultMaven Evidence System                      │
└──────────────────────────────────────────────────────────────────────────┘

┌─────────────┐
│   User      │
│ (Browser/   │
│  CLI/API)   │
└─────┬───────┘
      │
      │ HTTP POST
      │ (file or message)
      ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                        API Gateway Layer                                 │
│  POST /api/v1/cases/{case_id}/turns    (unified turn endpoint)         │
│  {query?, files[]?, pasted_content?}                                   │
└─────┬───────────────────────────────────────────────────────────────────┘
      │
      │
      ↓
┌─────────────────────────────────────────────────────────────────────────┐
│              File Preprocessing Layer (Tier 0 + Tier 1)                  │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ PreprocessingService.classify_and_extract(content, filename,       │ │
│  │                                             source_metadata)       │ │
│  │                                                                    │ │
│  │ 1. Tier 0: Classify data type → DataType enum + confidence        │ │
│  │    (propagates source_type from source_metadata)                  │ │
│  │ 2. Short-circuits:                                                 │ │
│  │      - UNANALYZABLE → placeholder (extraction_method=none)        │ │
│  │      - classification_failed → placeholder +                       │ │
│  │        suggested_types for cooperative-clarification UX           │ │
│  │      - source_type=page_capture → pass-through as structured MD   │ │
│  │ 3. Tier 1: Type-specific mechanical extraction (structural index) │ │
│  │    under 2s timeout; on timeout/error falls back to TEXT preview  │ │
│  │ 4. Compute content_hash (SHA-256 of UTF-8 text)                    │ │
│  │ 5. Return PreprocessingResult                                      │ │
│  │                                                                    │ │
│  │ Raw file persistence (storage_service.store_file) + per-case      │ │
│  │ content-hash dedup short-circuit happen at the evidence-creation  │ │
│  │ layer (_preprocess_attachment in InvestigationService). Dedup     │ │
│  │ calls ICaseRepository.find_by_content_hash before creating any    │ │
│  │ new Evidence row.                                                  │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────┬───────────────────────────────────────────────────────────────────┘
      │
      │ PreprocessingResult
      │ {data_type, summary, structural_index (= ExtractResult JSON), content_ref,
      │  content_hash, extraction_method, content_size_bytes, extraction_metadata}
      │
      │  ExtractResult JSON: {"v":1, "file_extract": "...", "search_map": "...",
      │                       "file_meta": {...}}
      │  Stored as evidence.preprocessed_content. Parsed by context_builder.py
      │  into three separate XML elements: <file_extract>, <search_map>, <file_meta>.
      ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                    Investigation Service Layer                           │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ process_turn(case_id, user_message, attachments)                  │ │
│  │                                                                     │ │
│  │ 1. Load case state from DB                                         │ │
│  │ 2. Add user message to case.messages[]                            │ │
│  │ 3. Build LLM prompt with full context                             │ │
│  │ 4. Call LLM service                                                │ │
│  │ 5. Process LLM response                                            │ │
│  │ 6. Create evidence if needed                                       │ │
│  │ 7. Update case state                                               │ │
│  │ 8. Return response to user                                         │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────┬───────────────────────────────────────────────────────────────────┘
      │
      │ LLM prompt
      │ (case context + user message)
      ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                         LLM Service Layer                                │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ LLM Evaluation (Anthropic Claude, OpenAI, etc.)                    │ │
│  │                                                                     │ │
│  │ Analyzes:                                                           │ │
│  │ - Full case context (existing evidence, hypotheses, milestones)   │ │
│  │ - User message content                                             │ │
│  │ - evidence.preprocessed_content parsed into three XML elements:   │ │
│  │     <file_extract> (orientation), <search_map> (entity profile +  │ │
│  │     search hints), <file_meta> (coverage stats as structured dict) │ │
│  │ - PreprocessingResult.summary (<500 chars, always included)       │ │
│  │                                                                     │ │
│  │ Returns:                                                           │ │
│  │ - evidence_to_add (category, data_type, summary, purpose)         │ │
│  │ - state_updates (hypotheses, milestones, etc.)                    │ │
│  │ - agent_response (natural language response)                      │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────┬───────────────────────────────────────────────────────────────────┘
      │
      │ BaseInteractionResponse
      │ {state_updates, evidence_to_add, ...}
      ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                Evidence Creation Decision Layer                          │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Two-step pipeline:                                                  │ │
│  │                                                                     │ │
│  │ Step 1 — Attachment intake (before LLM, Tier 0+1):                 │ │
│  │   Each attachment → _preprocess_attachment() → UploadedFile row    │ │
│  │   carrying summary, structural_index, data_type, coverage_*.       │ │
│  │   No Evidence is created at intake.                                │ │
│  │                                                                     │ │
│  │ Step 2 — Agent findings (after LLM call):                          │ │
│  │   For each item in evidence_to_add:                                │ │
│  │     1. Validate category (symptom / causal / mitigation /          │ │
│  │        solution_evidence) — reject otherwise                       │ │
│  │     2. Build Evidence row with source_file_id pointing at the      │ │
│  │        originating UploadedFile (or source_type=user_description   │ │
│  │        for chat-quote rows with no file)                           │ │
│  │     3. Persist; milestone advancement derives from the category    │ │
│  │                                                                     │ │
│  │ No evidence_to_add → no evidence created (query-only turn)         │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────┬───────────────────────────────────────────────────────────────────┘
      │
      │ Evidence object
      ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                        Persistence Layer                                 │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ PostgreSQL Database                                                 │ │
│  │                                                                     │ │
│  │ evidence table:                                                     │ │
│  │ - evidence_id (PK)                                                 │ │
│  │ - case_id (FK → cases)                                             │ │
│  │ - category (symptom/causal/mitigation/solution/contextual/rejected)│ │
│  │ - data_type (logs/metrics/configuration/code/text/image)         │ │
│  │ - summary, primary_purpose                                         │ │
│  │ - content_ref, content_hash                                        │ │
│  │ - original_filename (display name from upload)                    │ │
│  │ - collected_at_turn, collected_at, collected_by                   │ │
│  │ - related_hypotheses, advances_milestones                         │ │
│  │ - processing_mode, da_invocation_count                            │ │
│  │                                                                     │ │
│  │ Indices:                                                            │ │
│  │ - INDEX (case_id, collected_at_turn)                              │ │
│  │ - INDEX (case_id, content_hash)                                   │ │
│  │ (No UNIQUE constraint on case+turn — multiple evidence            │ │
│  │  items per turn are allowed; deduplication is done at the         │ │
│  │  application layer via content_hash lookup.)                      │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                      Async Failure Handling Layer                        │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Background Jobs                                                    │ │
│  │                                                                     │ │
│  │ storage_cleanup (faultmaven.modules.agent.jobs.storage_cleanup):  │ │
│  │   - TTL-based orphan-file sweep (default 24h)                     │ │
│  │   - Sidecar-driven: every stored file has a {name}.meta.json     │ │
│  │     sidecar; mark_linked() flips linked=true after Evidence       │ │
│  │     creation. Sweep deletes files whose sidecar says linked=false │ │
│  │     AND uploaded_at > TTL ago.                                    │ │
│  │   - Gated on orphan_cleanup_enabled + orphan_cleanup_dry_run      │ │
│  │     (default dry_run=True — 48h canary protocol required before   │ │
│  │     enabling real deletes in production)                          │ │
│  │   - CLI: python -m faultmaven.jobs.run storage_cleanup            │ │
│  │                                                                     │ │
│  │ Turn-level LLM failure handling is synchronous today — the API    │ │
│  │ endpoint returns specific error codes (LLM_OVER_CAPACITY,         │ │
│  │ RATE_LIMIT_EXCEEDED, LLM_TIMEOUT) with Retry-After headers. Async │ │
│  │ turn retry was considered and deferred — see evidence-failure-    │ │
│  │ modes.md for rationale.                                            │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                     Monitoring & Observability Layer                     │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Prometheus metrics (infrastructure/observability/                  │ │
│  │                     evidence_metrics.py):                          │ │
│  │                                                                     │ │
│  │ Live (emit sites active):                                          │ │
│  │   faultmaven_evidence_dedup_hits_total                             │ │
│  │     — per-case content-hash dedup short-circuits                   │ │
│  │   faultmaven_evidence_orphan_files_found_total                     │ │
│  │   faultmaven_evidence_orphan_files_deleted_total                   │ │
│  │     — emitted by the storage_cleanup sweep                         │ │
│  │                                                                     │ │
│  │ Scaffolded (registered; emit sites deferred until async-retry     │ │
│  │  plan is justified by telemetry):                                  │ │
│  │   faultmaven_evidence_turn_async_retry_{enqueued,outcome}_total    │ │
│  │   faultmaven_evidence_turn_async_retry_latency_seconds             │ │
│  │                                                                     │ │
│  │ Canonical alert definitions live in                                │ │
│  │ docs/operations/monitoring/evidence-metrics.md.                    │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Sequence Diagram: Turn with Attachment → Evidence Creation

```
User          API(/turns)    Investigation    Preprocessing    Storage    LLM         Database
 │              │                │                │             │          │             │
 │─POST turn───>│                │                │             │          │             │
 │ {query,      │                │                │             │          │             │
 │  files[]}    │                │                │             │          │             │
 │              │─process_turn──>│                │             │          │             │
 │              │ (TurnPayload)  │                │             │          │             │
 │              │                │                │             │          │             │
 │              │                │ ── STEP 1: PRE-LLM ──       │          │             │
 │              │                │                │             │          │             │
 │              │                │─preprocess─────>│             │          │             │
 │              │                │ attachment      │             │          │             │
 │              │                │                │──classify───│          │             │
 │              │                │                │  Tier 0     │          │             │
 │              │                │                │──extract────│          │             │
 │              │                │                │  Tier 1     │          │             │
 │              │                │                │──store──────>│          │             │
 │              │                │                │  raw file   │          │             │
 │              │                │<─UploadedFile──│             │          │             │
 │              │                │  (no Evidence) │             │          │             │
 │              │                │                │             │          │             │
 │              │                │ ── STEP 2: LLM INFERENCE ── │          │             │
 │              │                │                │             │          │             │
 │              │                │─call LLM───────────────────────────────>│             │
 │              │                │ (query +       │             │          │             │
 │              │                │  structural    │             │          │             │
 │              │                │  indexes via   │             │          │             │
 │              │                │  Context       │             │          │             │
 │              │                │  Sliding       │             │          │             │
 │              │                │  Window)       │             │          │             │
 │              │                │                │             │          │             │
 │              │                │<───────────────────────────────────────│             │
 │              │                │ response       │             │          │             │
 │              │                │ {evidence_to_  │             │          │             │
 │              │                │  add, ...}     │             │          │             │
 │              │                │                │             │          │             │
 │              │                │─create agent───┼─────────────┼──────────┼────────────>│
 │              │                │ evidence       │             │          │   INSERT    │
 │              │                │ form=SUBMITTED │             │          │             │
 │              │                │ _DATA          │             │          │             │
 │              │                │                │             │          │             │
 │              │<─TurnResponse──│                │             │          │             │
 │              │ {agent_response│                │             │          │             │
 │              │  attachments_  │                │             │          │             │
 │              │  processed}    │                │             │          │             │
 │              │                │                │             │          │             │
 │<─200 OK─────│                │                │             │          │             │
 │ TurnResponse │                │                │             │          │             │
 │              │                │                │             │          │             │
```

---

## Sequence Diagram: Query-Only Turn → No Evidence

```
User          API(/turns)    Investigation    LLM         Database
 │              │                │             │             │
 │─POST turn───>│                │             │             │
 │ {query:      │                │             │             │
 │  "Why is     │                │             │             │
 │   CPU high?"│                │             │             │
 │  files: []}  │                │             │             │
 │              │                │             │             │
 │              │─process_turn──>│             │             │
 │              │ (TurnPayload,  │             │             │
 │              │  no attachmts) │             │             │
 │              │                │             │             │
 │              │                │ (Step 1 skipped: no attachments)       │
 │              │                │             │             │
 │              │                │─call LLM───>│             │
 │              │                │  (query +   │             │
 │              │                │   context)  │             │
 │              │                │             │             │
 │              │                │<────────────│             │
 │              │                │  response   │             │
 │              │                │  {no        │             │
 │              │                │   evidence_ │             │
 │              │                │   to_add}   │             │
 │              │                │             │             │
 │              │<─TurnResponse──│             │             │
 │              │ {agent_response│             │             │
 │              │  attachments_  │             │             │
 │              │  processed:[]} │             │             │
 │              │                │             │             │
 │<─200 OK─────│                │             │             │
 │ TurnResponse │                │             │             │
 │              │                │             │             │
```

---

## Sequence Diagram: Duplicate Upload

Per-case content-hash deduplication is live. Before creating a new Evidence row, `_preprocess_attachment` calls `ICaseRepository.find_by_content_hash(case_id, content_hash)`. A match returns the existing Evidence and skips raw-file re-storage (no new write to the storage backend). The per-attachment `AttachmentResult` carries `duplicate_of` + `duplicate_turn` so the frontend can render a toast. Scope is per-case; same content uploaded to a different case proceeds as new Evidence.

```
User          API(/turns)    Investigation    Preprocessing    Case Repository
 │              │                │                │             │
 │─POST turn───>│                │                │             │
 │ {files:      │                │                │             │
 │  [same file]}│                │                │             │
 │              │─process_turn──>│                │             │
 │              │ (TurnPayload)  │                │             │
 │              │                │                │             │
 │              │                │─classify_and──>│             │
 │              │                │  _extract      │             │
 │              │                │                │─compute hash│
 │              │                │<───────────────│ (abc123)    │
 │              │                │  PreprocessingResult         │
 │              │                │                              │
 │              │                │─find_by_content_hash────────>│
 │              │                │  (case_id, abc123)           │
 │              │                │<─────────────────────────────│
 │              │                │  MATCH ev_xyz (turn 5)        │
 │              │                │                              │
 │              │                │  (skip Evidence creation,    │
 │              │                │   skip storage.store_file,   │
 │              │                │   emit metric                │
 │              │                │   evidence_dedup_hits_total) │
 │              │<─TurnResponse──│                              │
 │              │ AttachmentResult{                             │
 │              │  evidence_id=ev_xyz,                          │
 │              │  processing_status="duplicate",               │
 │              │  duplicate_of=ev_xyz,                         │
 │              │  duplicate_turn=5 }                           │
 │              │                │                              │
 │<─200 OK─────│                │                              │
 │ TurnResponse │                │                              │
```

---

## Sequence Diagram: Classification Failed → Cooperative Clarification

Triggered when Tier 0 classification produces `confidence < 0.50` — the file cannot be routed to an extractor with enough certainty to auto-accept. The agent still attempts to answer the user's query using its file-reading tools. After the turn runs, `InvestigationService._build_classification_clarification_suggestions` injects COOPERATIVE suggestions (pre-composed `query_submit` payloads) so the user can re-prompt the agent with the correct type using a single click. No frontend modal, no re-classification — just additional follow-up suggestions ahead of the engine's own follow-ups.

```
User          API(/turns)    Investigation    Preprocessing    Agent LLM
 │              │                │                │              │
 │─POST turn───>│                │                │              │
 │ {ambiguous.  │                │                │              │
 │  csv,        │                │                │              │
 │  query: "?"} │                │                │              │
 │              │─process_turn──>│                │              │
 │              │                │─classify_and──>│              │
 │              │                │  _extract      │              │
 │              │                │                │ conf=0.45    │
 │              │                │                │ failed=True  │
 │              │                │                │ suggested_   │
 │              │                │                │  types=      │
 │              │                │                │  [metrics,   │
 │              │                │                │   text]      │
 │              │                │<───────────────│              │
 │              │                │  PreprocessingResult          │
 │              │                │  extraction_method=           │
 │              │                │    "classification_failed"    │
 │              │                │  metadata.suggested_types=    │
 │              │                │    ["metrics_and_performance",│
 │              │                │     "unstructured_text"]      │
 │              │                │                               │
 │              │                │  (agent still runs — uses     │
 │              │                │   search_file/deep_analysis   │
 │              │                │   on raw bytes; produces      │
 │              │                │   best-effort answer)         │
 │              │                │─────────────────────────────> │
 │              │                │<──────────────────────────────│
 │              │                │                               │
 │              │                │ (post-turn injector builds    │
 │              │                │  COOPERATIVE suggestions from │
 │              │                │  suggested_types + "Something │
 │              │                │  else" fallback; prepends to  │
 │              │                │  suggested_actions)           │
 │              │<─TurnResponse──│                               │
 │              │  {                                              │
 │              │    suggested_actions: [                         │
 │              │      {type:"COOPERATIVE",                       │
 │              │       label:"Metrics",                          │
 │              │       payload:"Treat file as metrics..."},      │
 │              │      {type:"COOPERATIVE",                       │
 │              │       label:"Something else",                   │
 │              │       payload:"Treat as unstructured text..."}, │
 │              │      ...engine follow-ups                       │
 │              │    ]                                            │
 │              │  }                                              │
 │<─200 OK─────│                │                │               │
 │              │                                                 │
 │ (user clicks "Metrics" — frontend submits the suggestion's     │
 │  query_submit payload as the next user turn)                   │
 │─POST turn───>│                                                 │
 │ {query:"Treat the previously uploaded file as metrics..."}    │
 │              │─process_turn──>│ (normal turn, agent uses the  │
 │              │                │  type hint while reading the  │
 │              │                │  raw file via search_file)    │
```

---

## Sequence Diagram: UNANALYZABLE Short-Circuit

Triggered when the user has opted a file out of analysis (e.g., VISUAL_EVIDENCE with vision disabled, or an explicit UNANALYZABLE classification). The service returns a reference-only placeholder so the Evidence record exists (for audit / future access) without invoking any extractor.

```
User          API(/turns)    Investigation    Preprocessing
 │              │                │                │
 │─POST turn───>│                │                │
 │ {image.png,  │                │                │
 │  vision=off} │                │                │
 │              │─process_turn──>│                │
 │              │                │─classify_and──>│
 │              │                │  _extract      │
 │              │                │                │ Tier 0:
 │              │                │                │ UNANALYZABLE
 │              │                │<───────────────│
 │              │                │  PreprocessingResult
 │              │                │  extraction_method="none"
 │              │                │  content="[File 'image.png'
 │              │                │    marked as UNANALYZABLE —
 │              │                │    reference only...]"
 │              │<─TurnResponse──│                │
 │              │ (placeholder   │                │
 │              │  evidence,     │                │
 │              │  no extraction)│                │
 │<─200 OK─────│                │                │
```

---

## State Machine: Evidence Lifecycle

```
┌─────────────────────────────────────────────────────────────────┐
│                    Evidence Lifecycle                           │
└─────────────────────────────────────────────────────────────────┘

                            START
                              │
                              │ User submits file or message
                              ↓
                    ┌─────────────────────────┐
                    │ Step 1: Classification   │
                    │ (Tier 0+1, before LLM)  │
                    └────────┬────────────────┘
                             │
                             │
              ┌──────────────┴──────────────┐
              │                             │
              ↓                             ↓
     Has attachments?              No attachments
              │                             │
              ↓                             │
     ┌─────────────────┐                    │
     │ Each attachment:│                    │
     │ Tier 0+1        │                    │
     │ → UploadedFile  │                    │
     │   (no Evidence) │                    │
     └────────┬────────┘                    │
              │                             │
              │ Check duplicate             │
              │ (content_hash on            │
              │  uploaded_files)            │
              │                             │
         ┌────┴────┐                        │
         │ Dup?    │                        │
         └────┬────┘                        │
              │                             │
         Yes ─┤─ No                         │
              │    │                         │
              ↓    ↓                         │
     ┌──────────┐ UploadedFile               │
     │ Skip     │ persisted                  │
     │ (dedup)  │ (no Evidence yet)          │
     └──────────┘                           │
                                            │
              ┌─────────────────────────────┘
              │
              ↓
     ┌─────────────────┐
     │ LLM Evaluation  │
     └────────┬────────┘
              │
              │
     Has evidence_to_add?
              │
         ┌────┴────┐
         │         │
         ↓         ↓
        Yes       No
         │         │
         ↓         ↓
     ┌──────────┐  ┌──────────┐
     │ Each:    │  │ NO       │
     │ category │  │ EVIDENCE │
     │ ∈ {symp, │  │ from LLM │
     │ causal,  │  │ (query-  │
     │ mitig,   │  │  only    │
     │ soln_ev} │  │  turn)   │
     │          │  └──────────┘
     │ source_  │
     │ file_id  │
     │ → upload │
     └────┬─────┘
          │
     Persist Evidence
          │
          ↓
  Evidence rows
  created in DB
                    │
                    ↓
           ┌─────────────────┐
           │ Evidence        │
           │ Persisted       │
           └────────┬────────┘
                    │
                    ↓
             ┌──────────────┐
             │ Evidence     │
             │ Exists in DB │
             └──────┬───────┘
                    │
                    │ Case can query
                    │ for analytics
                    ↓
                   END
```

---

## Sequence Diagram: Tier 2/3 On-Demand Analysis (Agent Tool Calls)

```
User          API(/turns)    Investigation    Context      Deep Analysis   Storage
 │              │                │             │             │             │
 │─POST turn───>│                │             │             │             │
 │ {query:      │                │             │             │             │
 │  "What's in  │                │             │             │             │
 │   the stack  │                │             │             │             │
 │   trace at   │                │             │             │             │
 │   line       │                │             │             │             │
 │   12450?"}   │                │             │             │             │
 │              │─process_turn──>│             │             │             │
 │              │                │             │             │             │
 │              │                │─check───────>│             │             │
 │              │                │  evidence    │             │             │
 │              │                │  context     │             │             │
 │              │                │  (Sliding    │             │             │
 │              │                │   Window)    │             │             │
 │              │                │             │             │             │
 │              │                │<────────────│             │             │
 │              │                │  ev_abc:     │             │             │
 │              │                │  structural  │             │             │
 │              │                │  index       │             │             │
 │              │                │             │             │             │
 │              │                │ (Agent reasons: "Tier 1    │             │
 │              │                │  index has cluster summary │             │
 │              │                │  but not the actual stack  │             │
 │              │                │  trace. Need Tier 3.")     │             │
 │              │                │             │             │             │
 │              │                │─deep_analysis(─────────>│             │
 │              │                │  ev_abc,    │             │             │
 │              │                │  "extract   │             │             │
 │              │                │   stack     │             │             │
 │              │                │   trace")   │             │             │
 │              │                │             │             │             │
 │              │                │             │             │─retrieve───>│
 │              │                │             │             │  raw file   │
 │              │                │             │             │<────────────│
 │              │                │             │             │             │
 │              │                │             │             │─LLM/search─│
 │              │                │             │             │  raw file   │
 │              │                │             │             │             │
 │              │                │<──────────────────────────│             │
 │              │                │  DeepAnalysisResult       │             │
 │              │                │  {answer, excerpts}       │             │
 │              │                │             │             │             │
 │              │                │ (Agent incorporates Tier 3│             │
 │              │                │  result into response)    │             │
 │              │                │             │             │             │
 │              │<─TurnResponse──│             │             │             │
 │              │                │             │             │             │
 │<─200 OK─────│                │             │             │             │
 │ TurnResponse │                │             │             │             │
 │              │                │             │             │             │
```

**Key**: `search_file` and `deep_analysis` are invoked by the investigation agent as tool calls during `process_turn()`. The preprocessing service is NOT involved — it completed during Step 1 of the original turn. See [Data Preprocessing](./data-preprocessing-design-specification.md) Sections 3-4 for full invocation logic. Tool selection is guided by the processing mode (Triage vs Directed Analysis) set by the query classifier.

**DA Tool Loop (v5.0, updated v5.2)**: In Directed Analysis turns, the milestone engine routes inference through a bounded tool-calling loop (`_tool_augmented_generate()`) instead of single-shot generation. The LLM receives the investigation tools (`search_file`, `deep_analysis`, `kb_qa`, `web_search`, `case_evidence_search`) and the terminating `schema_tool`, iterating up to 4 times with an iteration-0 guardrail that forces at least one investigation-tool call before generating a structured response. The `search_file` tool resolves evidence content through dual-path resolution (standalone via `evidence_artifacts` table or case-embedded via `case_repo`). The `Evidence.original_filename` field provides the display filename in search results. See [Orchestration Capabilities §5.4](../investigation-engine/orchestration-capabilities.md#54-tool-augmented-generation-v50--v60) for full details.

**Orchestration Hardening (v4.2, updated v5.2)**: The orchestration layer adds three mechanical safety nets. See [Data Preprocessing §6.1](./data-preprocessing-design-specification.md#61-orchestration-hardening-mechanical-safety-nets-v42-updated-v52) for the canonical description.

- **Coverage gap detection (R3)**: Extracts entities (timestamps, services, error codes, IPs) from user queries and compares against evidence coverage metadata. Injects advisories when query entities fall outside evidence coverage.
- **Vectorization — proactive + reactive (R4, v5.2, gate tightened v5.4)**: For DA-mode turns *only* (gated on `force_tool_use=True` in `_tool_augmented_generate`), `_start_proactive_vectorization()` kicks off background `asyncio` tasks for every qualifying evidence file (size ≥ configured minimum, ≤ 50MB, not already vectorized) before the tool loop begins — so semantic search becomes available as the tool loop runs. Triage and Knowledge Query turns no longer trigger proactive embedding: they don't consult case evidence via semantic search, so the work would be wasted, and on a cold-cached BGE-M3 model it could starve the turn budget. Reactive fallback triggers (tool timeout, 3+ consecutive empty `search_file` results on the same evidence, `deep_analysis` confidence < 0.2) remain for cases where the proactive path wasn't taken or the agent's approach indicates point queries are insufficient. The `da_call_count >= 3` trigger was removed in v5.2. For small files below the vectorization threshold, raw content is injected directly into the LLM context instead. The primary `/turns` path uses simple per-evidence counters (`da_empty_search_counts`, `da_vectorized`); the secondary `/sessions/execute` path retains the v5.0 `EvidenceDAState` structure. Cross-turn DA history is reconstructed via the persisted `da_invocation_count` field on the Evidence model. Embedding runs on the thread-pool executor (`asyncio.to_thread`) so it never blocks the request event loop — see [data-preprocessing-design-specification §5.6.3](./data-preprocessing-design-specification.md#563-embedding-execution-and-fallback).
- **Context budget tracking (R5)**: Enforces a 30K character budget on tool results with standard/aggressive compression preserving high-signal lines (errors, exceptions, timeouts, crashes).

---

## Data Flow: INQUIRY Phase

During `INQUIRY` the user submits files to characterize the situation. No `Evidence` rows are created on intake — only `UploadedFile` rows carrying the preprocessing artifacts (`summary`, `structural_index`, `data_type`, coverage timestamps). The LLM reads files via `<uploaded_file file_id="...">` prompt blocks. When the case transitions to `INVESTIGATING` (the user confirms the problem statement), the LLM begins emitting `evidence_to_add` entries claim-by-claim; each new Evidence row carries a `source_file_id` back to the originating `UploadedFile`. There is no retroactive attribution sweep at the transition — milestones derive from evidence categories as rows are created turn-by-turn.

See `core/investigation/milestone_engine.py:_transition_to_investigating` for the transition handler.

---

## Failure Handling

LLM and DB-insert failure handling is synchronous in-process retry via `BaseExternalClient`. Terminal failures return specific error codes (`LLM_TIMEOUT`, `LLM_OVER_CAPACITY`, `RATE_LIMIT_EXCEEDED`) with `Retry-After` headers; the client retries the same turn. Orphan-file cleanup, dedup, and metric definitions live in [evidence-failure-modes.md](./evidence-failure-modes.md). No async-retry queue or worker exists; if one is ever justified by production telemetry, the design discussion will live in `evidence-failure-modes.md` rather than here.

---

## Key Design Decisions

The design decisions that govern the taxonomy and classification semantics live in their canonical documents. Pointers:

- **Evidence is claim-anchored.** The `evidence` table holds rows that the LLM emits with a category (`symptom_evidence` / `causal_evidence` / `mitigation_evidence` / `solution_evidence`) and either a `source_file_id` pointing at an `uploaded_files` row or `source_type=user_description` for chat-quote rows. File-level dedup is on `uploaded_files.content_hash`; the LLM never sees duplicate intake. See [evidence-driven-investigation-framework.md §5](../investigation-engine/evidence-driven-investigation-framework.md#5-evidence-model).
- **Strict category validation.** `EvidenceToAdd.validate_category` raises `ValidationError` on any value outside the four valid categories; the milestone engine's self-correction loop reprompts the LLM with the validator's message. See [Evidence Failure Modes → Scenario 3](./evidence-failure-modes.md) and `core/investigation/schemas.py:validate_category`.
- **No Evidence during INQUIRY; no retroactive attribution.** File uploads create `UploadedFile` rows only at intake. The LLM reads them via `<uploaded_file file_id="...">` prompt blocks and emits `evidence_to_add` once the case enters `INVESTIGATING`. Milestones derive from categories as rows are created turn-by-turn. See `core/investigation/milestone_engine.py:_transition_to_investigating`.
- **Source-discriminator lives on the row, not in a separate column.** `source_type` + `source_file_id` carry the source information together; the `evidence_source_invariant` DB CHECK requires `source_file_id IS NOT NULL OR source_type = 'user_description'`.

---

## Monitoring & Observability

### Key Metrics

Canonical Prometheus metric names (defined in `infrastructure/observability/evidence_metrics.py`):

**Live (emit sites active):**
- `faultmaven_evidence_dedup_hits_total` — per-case content-hash dedup short-circuits
- `faultmaven_evidence_orphan_files_found_total` — orphan files detected by the storage_cleanup sweep
- `faultmaven_evidence_orphan_files_deleted_total` — orphan files deleted by the storage_cleanup sweep

**Scaffolded (registered; emit sites deferred until async-retry plan is justified by telemetry):**
- `faultmaven_evidence_turn_async_retry_{enqueued,outcome}_total`
- `faultmaven_evidence_turn_async_retry_latency_seconds`

Alert definitions: `docs/operations/monitoring/evidence-metrics.md`.

### Dashboards

**Evidence Overview Dashboard:**
- Evidence created (time series)
- Category distribution (pie chart)
- Rejection rate (gauge)
- Acceptance rate by case (table)

**Failure Monitoring Dashboard:**
- LLM timeout rate (time series)
- Retry success rate (time series)
- Permanent failures (counter)
- Orphaned file cleanup (time series)

---

## Page Capture Pipeline (v2.6)

Page captures from the FaultMaven Copilot browser extension follow a distinct path: the extension pre-structures the live DOM into markdown (`htmlToStructuredText()`), the content is submitted via `POST /cases/{id}/turns` as `pasted_content` with `source_metadata.source_type = "page_capture"`, Tier 0 classifies it as `UNSTRUCTURED_TEXT`, Tier 1 is bypassed via the `page_capture_passthrough` branch, and the LLM system prompt contains page-capture format guidance.

For the canonical description of Stage 1 behaviour, pass-through branch, and format details, see [Data Preprocessing §2.4 — Pasted Text and Page Capture Processing](./data-preprocessing-design-specification.md#24-pasted-text-and-page-capture-processing).

---

## Related Documentation

- [Evidence Model](../investigation-engine/evidence-driven-investigation-framework.md#5-evidence-model) — Categories, source-type, `evidence_source_invariant`
- [Evidence Failure Modes](./evidence-failure-modes.md) — Failure handling for evidence creation
- [Data Preprocessing Design Specification](./data-preprocessing-design-specification.md) — Scenario-driven processing model, unified ingestion pipeline, query classifier, page capture pass-through, and orchestration hardening
- [Data Classification Strategy](./data-classification-strategy.md) — Tier 0 classification rules, source_type propagation

---

**Document Version:** 2.7
**Last Updated:** 2026-04-26
**Status:** Design Specification
