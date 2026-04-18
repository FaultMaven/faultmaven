# Evidence Flow Architecture

**Version:** 2.6
**Date:** 2026-03-15
**Status:** Design Specification

---

## Overview

This document describes the complete evidence flow architecture in FaultMaven. All user turns arrive via a unified endpoint (`POST /cases/{id}/turns`) and are processed through a two-step pipeline: (1) preprocess attachments through Tier 0+1 before the LLM, (2) LLM inference with structural indexes in context. Evidence form is payload-driven (attachments → `DOCUMENT`, agent findings → `SUBMITTED_DATA`). File preprocessing follows the [scenario-driven processing model](./data-preprocessing-design-specification.md). A mechanical query classifier routes each turn to Triage or Directed Analysis mode, which determines the system prompt and tool selection strategy. For DA-mode turns, vectorization is started proactively in the background for qualifying large files at the start of the tool loop; reactive fallback triggers remain for edge cases.

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
│  │      - classification_failed → placeholder (user modal)            │ │
│  │      - source_type=page_capture → pass-through as structured MD   │ │
│  │ 3. Tier 1: Type-specific mechanical extraction (structural index) │ │
│  │    under 2s timeout; on timeout/error falls back to TEXT preview  │ │
│  │ 4. Compute content_hash (SHA-256 of UTF-8 text)                    │ │
│  │ 5. Return PreprocessingResult                                      │ │
│  │                                                                    │ │
│  │ Raw file persistence (storage_service.store_file) happens at the  │ │
│  │ evidence-creation layer, not here. Dedup via content_hash is      │ │
│  │ not yet wired — see Deferred Items in the preprocessing spec.     │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────┬───────────────────────────────────────────────────────────────────┘
      │
      │ PreprocessingResult
      │ {data_type, summary, structural_index, content_ref, content_hash,
      │  extraction_method, content_size_bytes, extraction_metadata}
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
│  │ - PreprocessingResult.structural_index (if file upload)           │ │
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
│                    Evidence Creation Decision Layer                      │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Payload-driven unified ingestion pipeline:                         │ │
│  │                                                                     │ │
│  │ Step 1 — Classification + Attachments (before LLM, Tier 0+1):    │ │
│  │   All classification via Tier 0+1 (no LLM classification)        │ │
│  │   Each attachment → _preprocess_attachment() → Evidence            │ │
│  │   form=DOCUMENT, preprocessing_method from Tier 0+1               │ │
│  │                                                                     │ │
│  │ Step 2 — Agent findings (after LLM call):                         │ │
│  │   For each item in evidence_to_add:                               │ │
│  │     1. Compute content_hash for deduplication                     │ │
│  │     2. Create Evidence with form=SUBMITTED_DATA                   │ │
│  │     3. Infer milestone advancement                                │ │
│  │     4. Insert into case                                            │ │
│  │                                                                     │ │
│  │ No evidence_to_add → no evidence created (query-only turn)        │ │
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
│  │ - category (symptom/causal/resolution/contextual/rejected)        │ │
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
│  │ Celery Background Jobs (Retry Infrastructure)                      │ │
│  │                                                                     │ │
│  │ retry_evidence_analysis:                                           │ │
│  │   - Retry LLM call on timeout (exponential backoff: 1m, 2m, 4m)  │ │
│  │   - Max 3 retries, then create REJECTED                           │ │
│  │                                                                     │ │
│  │ retry_evidence_creation:                                           │ │
│  │   - Retry DB insert on failure (exponential backoff: 10s-160s)   │ │
│  │   - Max 5 retries, then alert ops                                 │ │
│  │   - Idempotency via content_hash check                            │ │
│  │                                                                     │ │
│  │ cleanup_orphaned_files:                                            │ │
│  │   - Daily job (2 AM UTC)                                           │ │
│  │   - Delete files >24h old with no evidence record                 │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                     Monitoring & Observability Layer                     │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ Metrics (Prometheus):                                              │ │
│  │ - evidence.created.total                                           │ │
│  │ - evidence.created.by_category{category}                          │ │
│  │ - evidence.rejected.total                                          │ │
│  │ - evidence.rejection_rate                                          │ │
│  │ - evidence.llm_timeouts                                            │ │
│  │ - evidence.retry_successes                                         │ │
│  │ - evidence.orphaned_files_cleaned                                  │ │
│  │                                                                     │ │
│  │ Alerts (Alertmanager):                                             │ │
│  │ - High LLM timeout rate (>5%)                                      │ │
│  │ - Permanent retry failures (>0)                                    │ │
│  │ - High rejection rate (>20%)                                       │ │
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
 │              │                │<─Evidence───────│             │          │             │
 │              │                │ form=DOCUMENT  │             │          │             │
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

## Sequence Diagram: Duplicate Upload (Deferred)

> **Status:** Duplicate detection is **not currently wired up**. `PreprocessingResult.content_hash` is computed for every attachment, but no repository consults it before evidence creation. Re-uploading the same file today produces a second Evidence row. Closing this requires `find_by_content_hash()` on the case/evidence repository plus a short-circuit in `_preprocess_attachment`. Tracked as a Deferred Item in the preprocessing spec; related failure-mode design in [evidence-failure-modes.md](./evidence-failure-modes.md).

The target flow, once dedup is implemented, looks like this:

```
User          API(/turns)    Investigation    Preprocessing    Evidence Repo
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
 │              │                │   return existing ev_xyz     │
 │              │                │   with status=duplicate)     │
 │              │<─TurnResponse──│                              │
 │              │ {status:       │                              │
 │              │  duplicate}    │                              │
 │              │                │                              │
 │<─200 OK─────│                │                              │
 │ TurnResponse │                │                              │
```

---

## Sequence Diagram: Classification Failed → User Modal

Triggered when Tier 0 classification produces `confidence < 0.50` — the file cannot be routed to an extractor with enough certainty to auto-accept. The frontend shows a modal for user selection, then resubmits with `user_override` set (Priority 1 of the 5-priority classifier, confidence 1.0).

```
User          API(/turns)    Investigation    Preprocessing    Frontend
 │              │                │                │             │
 │─POST turn───>│                │                │             │
 │ {ambiguous.  │                │                │             │
 │  csv}        │                │                │             │
 │              │─process_turn──>│                │             │
 │              │                │─classify_and──>│             │
 │              │                │  _extract      │             │
 │              │                │                │ conf=0.45   │
 │              │                │                │ failed=True │
 │              │                │<───────────────│             │
 │              │                │  PreprocessingResult         │
 │              │                │  extraction_method=          │
 │              │                │    "classification_failed"   │
 │              │                │  metadata.suggested_types=   │
 │              │                │    [metrics, text]           │
 │              │<─TurnResponse──│                │             │
 │              │ (placeholder   │                │             │
 │              │  evidence)     │                │             │
 │<─200 OK─────│                │                │             │
 │              │                │                │             │
 │──────────────┼────────────────┼────────────────┼────────────>│
 │              │                │                │  detect     │
 │              │                │                │  marker,    │
 │              │                │                │  show modal │
 │              │<───user picks──┼────────────────┼─────────────│
 │              │  type          │                │             │
 │─POST turn───>│ (same file +   │                │             │
 │              │  user_override)│                │             │
 │              │─process_turn──>│                │             │
 │              │                │─classify_and──>│             │
 │              │                │                │ Priority 1: │
 │              │                │                │ user_override│
 │              │                │                │ conf=1.0    │
 │              │                │<───────────────│ extractor   │
 │              │                │  success       │ runs        │
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

## Sequence Diagram: LLM Timeout → Async Retry

```
User          API          Investigation    LLM         Job Queue       Worker
 │              │                │             │             │             │
 │─POST file───>│                │             │             │             │
 │              │                │             │             │             │
 │              │─process_turn──>│             │             │             │
 │              │                │             │             │             │
 │              │                │─call LLM───>│             │             │
 │              │                │  (30s       │             │             │
 │              │                │   timeout)  │             │             │
 │              │                │             │             │             │
 │              │                │             │─(timeout)───│             │
 │              │                │             │  after 30s  │             │
 │              │                │             │             │             │
 │              │                │<────────────│             │             │
 │              │                │  LLMTimeout │             │             │
 │              │                │             │             │             │
 │              │                │─enqueue─────────────────>│             │
 │              │                │  retry job  │             │             │
 │              │                │  (case_id,  │             │             │
 │              │                │   content,  │             │             │
 │              │                │   retry=0)  │             │             │
 │              │                │             │             │             │
 │              │<─response──────│             │             │             │
 │              │  {status:      │             │             │             │
 │              │   analyzing}   │             │             │             │
 │              │                │             │             │             │
 │<─202────────│                │             │             │             │
 │  "Check back │                │             │             │             │
 │   shortly"   │                │             │             │             │
 │              │                │             │             │             │
 │              │                │             │             │             │
 │              │         (1 minute later)     │             │             │
 │              │                │             │             │<─pick job───│
 │              │                │             │             │             │
 │              │                │             │<────────────┼─────────────│
 │              │                │             │  retry LLM  │             │
 │              │                │             │  (60s       │             │
 │              │                │             │   timeout)  │             │
 │              │                │             │             │             │
 │              │                │             │─────────────┼───────────> │
 │              │                │             │  SUCCESS    │     create  │
 │              │                │             │  {category} │     evidence│
 │              │                │             │             │             │
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
     │ Each attachment: │                    │
     │ form=DOCUMENT    │                    │
     │ Tier 0+1 classif │                    │
     └────────┬────────┘                    │
              │                             │
              │ Check duplicate             │
              │ (content_hash)              │
              │                             │
         ┌────┴────┐                        │
         │ Dup?    │                        │
         └────┬────┘                        │
              │                             │
         Yes ─┤─ No                         │
              │    │                         │
              ↓    ↓                         │
     ┌──────────┐ Evidence                  │
     │ Skip     │ created                   │
     │ (dedup)  │ form=DOCUMENT             │
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
     │ form=    │  │ EVIDENCE │
     │ SUBMITTED│  │ from LLM │
     │ _DATA    │  │ (query-  │
     │          │  │  only    │
     │ Check    │  │  turn)   │
     │ duplicate│  └──────────┘
     └────┬─────┘
          │
     ┌────┴────┐
     │ Dup?    │
     └────┬────┘
          │
     Yes ─┤─ No
          │    │
          ↓    ↓
  ┌──────────┐ Evidence
  │ Skip     │ created
  │ (dedup)  │ form=SUBMITTED_DATA
  └──────────┘
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
 │              │                │─deep_analyze_file(─────────>│             │
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

**DA Tool Loop (v5.0, updated v5.2)**: In Directed Analysis turns, the milestone engine routes inference through a bounded tool-calling loop (`_tool_augmented_generate()`) instead of single-shot generation. The LLM receives the investigation tools (`search_file`, `deep_analysis`, `kb_qa`, `web_search`, `case_evidence_search`) and the terminating `schema_tool`, iterating up to 4 times with an iteration-0 guardrail that forces at least one investigation-tool call before generating a structured response. The `search_file` tool resolves evidence content through dual-path resolution (standalone via `evidence_artifacts` table or case-embedded via `case_repo`). The `Evidence.original_filename` field provides the display filename in search results. See [Orchestration Capabilities §5.4](../investigation-engine/orchestration-capabilities.md#54-da-tool-loop-bounded-tool-calling-v50) for full details.

**Orchestration Hardening (v4.2, updated v5.2)**: The orchestration layer adds three mechanical safety nets. See [Data Preprocessing §6.1](./data-preprocessing-design-specification.md#61-orchestration-hardening-mechanical-safety-nets-v42-updated-v52) for the canonical description.

- **Coverage gap detection (R3)**: Extracts entities (timestamps, services, error codes, IPs) from user queries and compares against evidence coverage metadata. Injects advisories when query entities fall outside evidence coverage.
- **Vectorization — proactive + reactive (R4, v5.2)**: For DA-mode turns, `_start_proactive_vectorization()` kicks off background `asyncio` tasks for every qualifying evidence file (size ≥ configured minimum, ≤ 50MB, not already vectorized) before the tool loop begins — so semantic search becomes available as the tool loop runs. Reactive fallback triggers (tool timeout, 3+ consecutive empty `search_file` results on the same evidence, `deep_analysis` confidence < 0.2) remain for cases where the proactive path wasn't taken or the agent's approach indicates point queries are insufficient. The `da_call_count >= 3` trigger was removed in v5.2. For small files below the vectorization threshold, raw content is injected directly into the LLM context instead. The primary `/turns` path uses simple per-evidence counters (`da_empty_search_counts`, `da_vectorized`); the secondary `/sessions/execute` path retains the v5.0 `EvidenceDAState` structure. Cross-turn DA history is reconstructed via the persisted `da_invocation_count` field on the Evidence model.
- **Context budget tracking (R5)**: Enforces a 30K character budget on tool results with standard/aggressive compression preserving high-signal lines (errors, exceptions, timeouts, crashes).

---

## Data Flow: INQUIRY Phase Classification

**Scenario:** User uploads log file during INQUIRY phase (before investigation starts)

```
┌──────────────────────────────────────────────────────────────────┐
│ Turn 1 (INQUIRY Phase)                                           │
│                                                                  │
│ User: "Can you check this log file?"                           │
│ *uploads app.log with connection timeout errors*                │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ↓
                  ┌───────────────────────┐
                  │ LLM Evaluates Content │
                  └───────────┬───────────┘
                              │
                              │ Classifies based on CONTENT,
                              │ not phase
                              ↓
          "This log shows connection timeout errors"
                              │
                              ↓
                  ┌───────────────────────┐
                  │ Category:             │
                  │ SYMPTOM_EVIDENCE      │
                  │                       │
                  │ (based on what data   │
                  │  CONTAINS, not phase) │
                  └───────────┬───────────┘
                              │
                              ↓
                  ┌───────────────────────┐
                  │ Evidence Created:     │
                  │ - category: SYMPTOM   │
                  │ - collected_at_turn: 1│
                  │ - advances_milestones:│
                  │   [] (empty)          │
                  │                       │
                  │ (No milestone         │
                  │  validation during    │
                  │  INQUIRY)             │
                  └───────────┬───────────┘
                              │
                              │
┌─────────────────────────────┴────────────────────────────────────┐
│ Turn 2 (INQUIRY → INVESTIGATING)                                 │
│                                                                  │
│ User: "This looks bad, let's investigate"                       │
│ Status: INQUIRY → INVESTIGATING                                 │
└──────────────────────────────────────────────────────────────────┘
                              │
                              │
┌─────────────────────────────┴────────────────────────────────────┐
│ Turn 3 (INVESTIGATING Phase)                                     │
│                                                                  │
│ User uploads additional evidence: connection pool config         │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ↓
                  ┌───────────────────────┐
                  │ Milestone Engine:     │
                  │ NOW ACTIVE            │
                  └───────────┬───────────┘
                              │
                              ↓
                  ┌───────────────────────┐
                  │ MilestoneUpdates:     │
                  │ - symptom_verified    │
                  │ - scope_assessed      │
                  └───────────┬───────────┘
                              │
                              ↓
                  ┌───────────────────────┐
                  │ System Infers:        │
                  │                       │
                  │ Evidence from turn 1: │
                  │ advances_milestones = │
                  │ ["symptom_verified",  │
                  │  "scope_assessed"]    │
                  │                       │
                  │ (Evidence retroactively│
                  │  contributes)         │
                  └───────────────────────┘
```

This diagram illustrates the retroactive milestone-advancement flow. The underlying rule (classify by content, not phase) is documented canonically in [Evidence Classification Design → INQUIRY Phase Classification](./evidence-classification-design.md#inquiry-phase-classification-first-class-scenario).

---

## Failure Handling Flow

### LLM Timeout Scenario

```
┌─────────────────────────────────────────────────────────────────┐
│                    LLM Timeout Handling                         │
└─────────────────────────────────────────────────────────────────┘

File Upload
    │
    ↓
Preprocessing (success)
    │
    ↓
Upload to S3 (success)
    │
    ↓
Call LLM (30s timeout)
    │
    │──────────> (30 seconds pass)
    │
    ↓
TIMEOUT!
    │
    ├─────────────────────────┐
    │                         │
    ↓                         ↓
Return to User          Queue Retry Job
"Analyzing, check       ├─ retry_count: 0
 back shortly"          ├─ max_retries: 3
                        ├─ delay: 1 minute
                        └─ content_ref, hash
                              │
                              │ (1 min later)
                              ↓
                        Worker picks job
                              │
                              ↓
                        Call LLM (60s timeout)
                              │
                      ┌───────┴────────┐
                      │                │
                      ↓                ↓
                   SUCCESS          TIMEOUT
                      │                │
                      ↓                ↓
              Create Evidence   Retry again
              Insert DB         (2 min delay)
                                      │
                                ┌─────┴────────┐
                                │              │
                                ↓              ↓
                             SUCCESS       TIMEOUT
                                │              │
                                ↓              ↓
                        Create Evidence   Retry again
                        Insert DB         (4 min delay)
                                              │
                                        ┌─────┴─────┐
                                        │           │
                                        ↓           ↓
                                    SUCCESS     TIMEOUT
                                        │           │
                                        ↓           ↓
                                Create         Max retries
                                Evidence       reached (3)
                                                   │
                                                   ↓
                                            Create REJECTED
                                            evidence
                                            "Analysis failed
                                             after retries"
```

### DB Insert Failure Scenario

```
┌─────────────────────────────────────────────────────────────────┐
│                 Database Insert Failure Handling                │
└─────────────────────────────────────────────────────────────────┘

LLM Analysis (success)
    │
    ├─ LLM result: {category, summary, ...}
    │
    ↓
Create Evidence Object
    │
    ↓
Insert to Database
    │
    ↓
DATABASE ERROR!
    │
    ├─────────────────────────┐
    │                         │
    ↓                         ↓
Return to User          Queue Retry Job
"Processing, will       ├─ retry_count: 0
 appear shortly"        ├─ max_retries: 5
                        ├─ delay: 10 seconds
                        └─ llm_result (serialized)
                              │
                              │ (10 sec later)
                              ↓
                        Worker picks job
                              │
                              ↓
                        Check if exists
                        (idempotency via
                         content_hash)
                              │
                        ┌─────┴──────┐
                        │            │
                        ↓            ↓
                    Not exists   Exists
                        │            │
                        ↓            ↓
                    Retry       Success
                    INSERT      (skip)
                        │
                    ┌───┴────┐
                    │        │
                    ↓        ↓
                SUCCESS   FAILURE
                    │        │
                    ↓        ↓
                  DONE    Retry again
                         (20s delay)
                              │
                         (max 5 retries)
                              │
                              ↓
                    Max retries reached
                              │
                              ↓
                    CRITICAL ALERT
                    (ops team notified)
```

---

## Key Design Decisions

The design decisions that govern the taxonomy and classification semantics live in their canonical documents. Pointers:

- **Evidence table includes REJECTED submissions** (deduplication, audit trail, cost efficiency, user feedback) — see [Evidence Classification Design → Evidence Table Semantics](./evidence-classification-design.md#evidence-table-semantics).
- **Category validation with `CONTEXTUAL_EVIDENCE` fallback** for unrecognized LLM-generated categories — see [Evidence Failure Modes → Scenario 3](./evidence-failure-modes.md).
- **Classification based on content, not phase** (INQUIRY-phase evidence contributes retroactively when investigation starts) — see [Evidence Classification Design → INQUIRY Phase Classification](./evidence-classification-design.md#inquiry-phase-classification-first-class-scenario).
- **System-inferred milestone advancement (Option 2.5)** via `CATEGORY_MILESTONE_MAP` — see [Evidence Classification Design → Milestone Advancement Attribution](./evidence-classification-design.md#milestone-advancement-attribution).

---

## Monitoring & Observability

### Key Metrics

```
# Evidence creation
evidence.created.total
evidence.created.by_category{category="symptom_evidence"}
evidence.created.by_category{category="rejected"}
evidence.rejection_rate

# Failures
evidence.llm_timeouts
evidence.llm_errors
evidence.db_insert_failures
evidence.retry_attempts
evidence.retry_successes
evidence.retry_permanent_failures

# Storage
evidence.orphaned_files_cleaned
evidence.storage_size_bytes
```

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

- [Evidence Classification Design](./evidence-classification-design.md) — Evidence taxonomy, categories, and DataType enum
- [Evidence Failure Modes](./evidence-failure-modes.md) — Failure handling for single-phase creation
- [Data Preprocessing Design Specification](./data-preprocessing-design-specification.md) — Scenario-driven processing model, unified ingestion pipeline, query classifier, page capture pass-through, and orchestration hardening
- [Data Classification Strategy](./data-classification-strategy.md) — Tier 0 classification rules, source_type propagation

---

**Document Version:** 2.6
**Last Updated:** 2026-03-15
**Status:** Design Specification
