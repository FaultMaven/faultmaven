# Evidence Flow Architecture

**Version:** 2.2
**Date:** 2026-02-12
**Status:** Design Specification

---

## Overview

This document describes the complete evidence flow architecture in FaultMaven. Evidence is created in a **single phase** after LLM evaluation. File preprocessing follows the [three-tier model](./data-preprocessing-design-specification.md) (Tier 0 classification + Tier 1 mechanical extraction). Tier 2 deep analysis is invoked on-demand by the investigation agent, not at upload time.

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
│  POST /api/v1/cases/{case_id}/data     (file upload)                   │
│  POST /api/v1/cases/{case_id}/queries  (text message)                  │
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
│  │ Returns:                                                            │ │
│  │ - submission_classification (user_text/submitted_data/mixed)       │ │
│  │ - evidence_to_add (category, data_type, summary, purpose)         │ │
│  │ - state_updates (hypotheses, milestones, etc.)                    │ │
│  │ - agent_response (natural language response)                      │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────┬───────────────────────────────────────────────────────────────────┘
      │
      │ BaseInteractionResponse
      │ {submission_classification, state_updates, ...}
      ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                    Evidence Creation Decision Layer                      │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ if submission_classification.type == "user_text":                  │ │
│  │     NO evidence created                                            │ │
│  │     Message stays in case.messages[] only                          │ │
│  │                                                                     │ │
│  │ elif submission_classification.type in ["submitted_data", "mixed"]: │ │
│  │     Create evidence record:                                        │ │
│  │     1. Check for duplicate (content_hash)                         │ │
│  │     2. If duplicate: Create REJECTED with reference               │ │
│  │     3. If unique: Create with LLM-provided category               │ │
│  │        (invalid categories fall back to CONTEXTUAL_EVIDENCE)     │ │
│  │     4. Infer milestone advancement                                │ │
│  │     5. Insert into database                                        │ │
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

## Sequence Diagram: File Upload → Evidence Creation

```
User          API          Preprocessing    Storage    Investigation    LLM         Database
 │              │                │             │            │             │             │
 │─POST file───>│                │             │            │             │             │
 │              │                │             │            │             │             │
 │              │─preprocess────>│             │            │             │             │
 │              │                │             │            │             │             │
 │              │                │──compute────│            │             │             │
 │              │                │  hash       │            │             │             │
 │              │                │             │            │             │             │
 │              │                │──check──────┼───────────>│             │             │
 │              │                │  duplicate  │            │             │             │
 │              │                │             │            │─query DB───>│             │
 │              │                │             │            │  (hash)     │             │
 │              │                │             │            │<────────────│             │
 │              │                │             │            │  no match   │             │
 │              │                │<────────────┼────────────│             │             │
 │              │                │  not dup    │            │             │             │
 │              │                │             │            │             │             │
 │              │                │──extract────│            │             │             │
 │              │                │  text       │            │             │             │
 │              │                │             │            │             │             │
 │              │                │──upload─────>│            │             │             │
 │              │                │  file (TTL) │            │             │             │
 │              │                │             │            │             │             │
 │              │<─metadata──────│             │            │             │             │
 │              │  {hash, text}  │             │            │             │             │
 │              │                │             │            │             │             │
 │              │─process_turn──────────────────────────────>│             │             │
 │              │  (message,     │             │            │             │             │
 │              │   attachments) │             │            │             │             │
 │              │                │             │            │             │             │
 │              │                │             │            │─call LLM───>│             │
 │              │                │             │            │  (prompt +  │             │
 │              │                │             │            │   context)  │             │
 │              │                │             │            │             │             │
 │              │                │             │            │             │─analyze────>│
 │              │                │             │            │             │  content    │
 │              │                │             │            │             │             │
 │              │                │             │            │<────────────│             │
 │              │                │             │            │  response   │             │
 │              │                │             │            │  {category, │             │
 │              │                │             │            │   type,...} │             │
 │              │                │             │            │             │             │
 │              │                │             │            │─create──────┼────────────>│
 │              │                │             │            │  evidence   │    INSERT   │
 │              │                │             │            │             │             │
 │              │                │             │            │<────────────┼─────────────│
 │              │                │             │            │  success    │             │
 │              │                │             │            │             │             │
 │              │<───response────────────────────────────────│             │             │
 │              │  {evidence_id, │             │            │             │             │
 │              │   category}    │             │            │             │             │
 │              │                │             │            │             │             │
 │<─201────────│                │             │            │             │             │
 │  Created     │                │             │            │             │             │
 │  {evidence}  │                │             │            │             │             │
 │              │                │             │            │             │             │
```

---

## Sequence Diagram: Chat Message → No Evidence

```
User          API          Investigation    LLM         Database
 │              │                │             │             │
 │─POST query──>│                │             │             │
 │  "Why is    │                │             │             │
 │   CPU high?" │                │             │             │
 │              │                │             │             │
 │              │─process_turn──>│             │             │
 │              │  (message)     │             │             │
 │              │                │             │             │
 │              │                │─call LLM───>│             │
 │              │                │  (prompt)   │             │
 │              │                │             │             │
 │              │                │<────────────│             │
 │              │                │  response   │             │
 │              │                │  {type:     │             │
 │              │                │   user_text}│             │
 │              │                │             │             │
 │              │                │─decision────│             │
 │              │                │  NO evidence│             │
 │              │                │  created    │             │
 │              │                │             │             │
 │              │<─response──────│             │             │
 │              │  {evidence:    │             │             │
 │              │   null}        │             │             │
 │              │                │             │             │
 │<─200 OK─────│                │             │             │
 │  {no         │                │             │             │
 │   evidence}  │                │             │             │
 │              │                │             │             │
```

---

## Sequence Diagram: Duplicate Upload

```
User          API          Preprocessing    Investigation    Database
 │              │                │                │             │
 │─POST file───>│                │                │             │
 │  (same file) │                │                │             │
 │              │                │                │             │
 │              │─preprocess────>│                │             │
 │              │                │                │             │
 │              │                │──compute hash──│             │
 │              │                │  (abc123)      │             │
 │              │                │                │             │
 │              │                │──check─────────>│             │
 │              │                │  duplicate     │             │
 │              │                │                │─query──────>│
 │              │                │                │  hash=      │
 │              │                │                │  abc123     │
 │              │                │                │             │
 │              │                │                │<────────────│
 │              │                │                │  MATCH      │
 │              │                │                │  ev_xyz     │
 │              │                │<───────────────│  (turn 5)   │
 │              │                │  duplicate     │             │
 │              │<─early return──│  found         │             │
 │              │                │                │             │
 │<─200 OK─────│                │                │             │
 │  {status:    │                │                │             │
 │   duplicate, │                │                │             │
 │   evidence_  │                │                │             │
 │   ref}       │                │                │             │
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
                    ┌─────────────────┐
                    │ Preprocessing   │
                    │ (file upload)   │
                    └────────┬────────┘
                             │
                             │ content_hash computed
                             ↓
                    ┌─────────────────┐
                    │ LLM Evaluation  │
                    └────────┬────────┘
                             │
                             │
              ┌──────────────┼──────────────┐
              │              │              │
              ↓              ↓              ↓
     submission_type   submission_type   submission_type
     = user_text       = submitted_data  = mixed
              │              │              │
              ↓              ↓              ↓
     ┌──────────┐    ┌─────────────┐  ┌─────────────┐
     │ NO       │    │ Check       │  │ Check       │
     │ EVIDENCE │    │ Duplicate   │  │ Duplicate   │
     │          │    └──────┬──────┘  └──────┬──────┘
     │ (stays   │           │                │
     │  in      │           │                │
     │  messages│      ┌────┴────┐      ┌────┴────┐
     │  only)   │      │ Dup?    │      │ Dup?    │
     └──────────┘      └────┬────┘      └────┬────┘
                            │                │
                       Yes ─┤                ├─ No
                            │                │
                            ↓                ↓
                   ┌─────────────┐   ┌────────────────┐
                   │ Create      │   │ Create         │
                   │ REJECTED    │   │ with LLM       │
                   │ with ref    │   │ category       │
                   └──────┬──────┘   └────────┬───────┘
                          │                   │
                          │                   │
                          └────────┬──────────┘
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

## Sequence Diagram: Tier 2 Deep Analysis (On-Demand)

```
User          API          Investigation    Vector DB    Tier2 Service   Storage
 │              │                │             │             │             │
 │─POST query──>│                │             │             │             │
 │ "What's in   │                │             │             │             │
 │  the stack   │                │             │             │             │
 │  trace at    │                │             │             │             │
 │  line 12450?"│                │             │             │             │
 │              │                │             │             │             │
 │              │─process_turn──>│             │             │             │
 │              │                │             │             │             │
 │              │                │─search──────>│             │             │
 │              │                │  "stack      │             │             │
 │              │                │   trace      │             │             │
 │              │                │   12450"     │             │             │
 │              │                │             │             │             │
 │              │                │<────────────│             │             │
 │              │                │  ev_abc:     │             │             │
 │              │                │  Cluster 1   │             │             │
 │              │                │             │             │             │
 │              │                │ (Agent reasons: "Tier 1    │             │
 │              │                │  index has cluster summary │             │
 │              │                │  but not the actual stack  │             │
 │              │                │  trace. Need Tier 2.")     │             │
 │              │                │             │             │             │
 │              │                │─analyze(────────────────-->│             │
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
 │              │                │ (Agent incorporates Tier 2│             │
 │              │                │  result into response)    │             │
 │              │                │             │             │             │
 │              │<─response──────│             │             │             │
 │              │                │             │             │             │
 │<─200 OK─────│                │             │             │             │
 │  {analysis   │                │             │             │             │
 │   with stack │                │             │             │             │
 │   trace}     │                │             │             │             │
```

**Key**: Tier 2 is invoked by the investigation agent as a tool call during `process_turn()`. The preprocessing service is NOT involved — it completed during the original file upload. See [Data Preprocessing v3.2](./data-preprocessing-design-specification.md) Section 6.1 for full invocation logic.

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
- [Data Preprocessing Design Specification v3.0](./data-preprocessing-design-specification.md) — Three-tier preprocessing model
- [Data Classification Strategy v2.0](./data-classification-strategy.md) — Tier 0 classification rules

---

**Document Version:** 2.2
**Last Updated:** 2026-02-12
**Status:** Design Specification
