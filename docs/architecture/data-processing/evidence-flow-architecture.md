# Evidence Flow Architecture

**Version:** 2.4
**Date:** 2026-02-23
**Status:** Design Specification

---

## Overview

This document describes the complete evidence flow architecture in FaultMaven. All user turns arrive via a unified endpoint (`POST /cases/{id}/turns`) and are processed through a two-step pipeline: (1) preprocess attachments through Tier 0+1 before the LLM, (2) LLM inference with structural indexes in context. Evidence form is payload-driven (attachments → `DOCUMENT`, agent findings → `SUBMITTED_DATA`). File preprocessing follows the [four-tier model](./data-preprocessing-design-specification.md). Tier 2 mechanical search and Tier 3 deep analysis are invoked on-demand by the investigation agent.

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
│  │ 1. Compute content_hash (SHA-256)                                  │ │
│  │ 2. Check for duplicate (early exit if hash exists)                │ │
│  │ 3. Tier 0: Classify data type → DataType enum + confidence        │ │
│  │ 4. Tier 1: Type-specific mechanical extraction (structural index) │ │
│  │ 5. Upload raw file to S3 with TTL metadata (24h)                  │ │
│  │ 6. Generate PreprocessingResult (summary, structural_index, etc.) │ │
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
│  │ - collected_at_turn, collected_at, collected_by                   │ │
│  │ - related_hypotheses, advances_milestones                         │ │
│  │                                                                     │ │
│  │ Constraints:                                                        │ │
│  │ - UNIQUE (case_id, collected_at_turn)                             │ │
│  │ - UNIQUE (case_id, content_hash)                                  │ │
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

## Sequence Diagram: Duplicate Upload

```
User          API(/turns)    Investigation    Preprocessing    Database
 │              │                │                │             │
 │─POST turn───>│                │                │             │
 │ {files:      │                │                │             │
 │  [same file]}│                │                │             │
 │              │─process_turn──>│                │             │
 │              │ (TurnPayload)  │                │             │
 │              │                │                │             │
 │              │                │─preprocess─────>│             │
 │              │                │ attachment      │             │
 │              │                │                │──compute────│
 │              │                │                │  hash       │
 │              │                │                │  (abc123)   │
 │              │                │                │             │
 │              │                │                │──check──────>│
 │              │                │                │  duplicate  │
 │              │                │                │             │
 │              │                │                │<────────────│
 │              │                │                │  MATCH      │
 │              │                │                │  ev_xyz     │
 │              │                │<───────────────│  (turn 5)   │
 │              │                │  duplicate     │             │
 │              │                │  found         │             │
 │              │                │                │             │
 │              │<─TurnResponse──│                │             │
 │              │ {status:       │                │             │
 │              │  duplicate}    │                │             │
 │              │                │                │             │
 │<─200 OK─────│                │                │             │
 │ TurnResponse │                │                │             │
 │              │                │                │             │
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

**Key**: Tier 2 (`search_file`) and Tier 3 (`deep_analyze_file`) are invoked by the investigation agent as tool calls during `process_turn()`. The preprocessing service is NOT involved — it completed during Step 1 of the original turn. See [Data Preprocessing v4.2](./data-preprocessing-design-specification.md) Sections 3-4 for full invocation logic.

**Tier-Escalation Hardening (v4.2)**: The orchestration layer adds three mechanical safety nets to improve tier escalation decisions:

- **Coverage gap detection (R3)**: Extracts entities (timestamps, services, error codes, IPs) from user queries and compares against evidence coverage metadata. Injects advisories when query entities fall outside evidence coverage.
- **Auto-escalation (R4)**: Tracks consecutive empty `search_file` results. After 2 consecutive zero-result calls, injects `[ESCALATION ADVISORY]` suggesting regex mode, deep analysis escalation, or vocabulary-guided retry.
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

**Key Insights:**
1. Evidence classified during INQUIRY based on content
2. Milestones NOT validated during INQUIRY
3. When investigation begins, existing evidence contributes to milestone advancement
4. Evidence created in INQUIRY "sits inert" until INVESTIGATING status

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

### 1. Evidence Table Includes Rejected Submissions

**Decision:** The `evidence` table tracks ALL file upload attempts, including rejected ones.

**Rationale:**
- Deduplication (prevent re-uploading same rejected file)
- Audit trail (complete record of what was submitted)
- Cost efficiency (avoid re-analyzing rejected files)
- User feedback (explain why rejected)

**Implementation:** Add `REJECTED` category to track rejected submissions.

**Semantic Note:** The table is called `evidence` for historical reasons, but conceptually represents "analyzed submissions" (both accepted and rejected).

---

### 2. Category Validation with Fallback

**Decision:** Use `CONTEXTUAL_EVIDENCE` as fallback for unrecognized LLM-generated categories.

**Rationale:**
- Not REJECTED (user uploaded intentionally)
- Not SYMPTOM/CAUSAL/RESOLUTION (avoid false positive milestone advancement)
- CONTEXTUAL is neutral ("we have this data, classification unclear")

**Implementation:**
```python
@validator('category', pre=True)
def validate_category(cls, v):
    if isinstance(v, str):
        try:
            return EvidenceCategory(v)
        except ValueError:
            logger.warning(f"LLM returned unrecognized category '{v}', falling back to CONTEXTUAL_EVIDENCE")
            return EvidenceCategory.CONTEXTUAL_EVIDENCE
    return v
```

See [Evidence Failure Modes - Scenario 3](./evidence-failure-modes.md) for full failure recovery details.

---

### 3. Classification Based on Content, Not Phase

**Decision:** Classify evidence based on what the data CONTAINS, not the investigation phase.

**Rationale:**
- Log file with errors = SYMPTOM_EVIDENCE (even during INQUIRY)
- Clean logs = CONTEXTUAL_EVIDENCE (even during INQUIRY)
- Milestone advancement happens later when investigation begins

**Implementation:** LLM classifies content directly, milestone validation only runs during INVESTIGATING status.

---

### 4. System-Inferred Milestone Advancement (Option 2.5)

**Decision:** System infers `advances_milestones` by default, LLM can override.

**Rationale:**
- 90% of cases: System inference sufficient (category → milestones mapping)
- 10% of cases: LLM can override when inference would be wrong
- Zero token cost for common cases
- Deterministic inference, no inconsistency risk

**Implementation:**
```python
advances_milestones = intersection(
    CATEGORY_MILESTONE_MAP[category],
    milestones_completed_this_turn
)
```

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

## Related Documentation

- [Evidence Classification Design](./evidence-classification-design.md) — Evidence taxonomy, categories, and DataType enum
- [Evidence Failure Modes](./evidence-failure-modes.md) — Failure handling for single-phase creation
- [Data Preprocessing Design Specification v4.2](./data-preprocessing-design-specification.md) — Four-tier preprocessing model, unified ingestion pipeline, and tier-escalation hardening
- [Data Classification Strategy v2.0](./data-classification-strategy.md) — Tier 0 classification rules

---

**Document Version:** 2.4
**Last Updated:** 2026-02-23
**Status:** Design Specification
