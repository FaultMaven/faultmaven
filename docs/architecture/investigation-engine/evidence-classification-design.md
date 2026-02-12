# Evidence Classification and Data Submission Design

> **DESIGN SUPERSEDED AND IMPLEMENTED** (2026-02-11):
>
> This document has been superseded by the final approved design, which is now **IMPLEMENTED**:
> **[EVIDENCE-CLASSIFICATION-FINAL-DESIGN.md](../data-processing/EVIDENCE-CLASSIFICATION-FINAL-DESIGN.md)** ✅ IMPLEMENTED
>
> **Implementation Summary:**
> **[EVIDENCE-REDESIGN-CHANGELOG.md](../data-processing/EVIDENCE-REDESIGN-CHANGELOG.md)**
>
> **Critical Changes (IMPLEMENTED 2026-02-11)**:
>
> 1. `EvidenceCategory.UNCLASSIFIED` → **REMOVED** (single-phase creation, evidence created AFTER LLM)
> 2. `EvidenceCategory.OTHER` → **RENAMED** to `CONTEXTUAL_EVIDENCE` (clearer purpose)
> 3. `EvidenceCategory.REJECTED` → **ADDED** (track rejected submissions, not IRRELEVANT)
> 4. `EvidenceSourceType`: 12 types → **5 types** (LOGS, METRICS, CONFIGURATION, VISUAL, USER_DESCRIPTION)
> 5. Evidence table now tracks ALL file submissions (including rejected)
> 6. Pure chat never enters evidence table
> 7. **Option 2.5 milestone attribution** - System-inferred with LLM override
>
> **See the final design document and changelog for complete specification.**

**Version**: 1.0 (Deprecated)
**Status**: ~~Design Specification~~ **SUPERSEDED** (Implemented 2026-02-11)
**Last Updated**: 2026-02-11
**Authors**: System Architecture Team

---

## Table of Contents

1. [Overview](#overview)
2. [Key Design Decisions](#key-design-decisions)
3. [Evidence Classification Flow](#evidence-classification-flow)
4. [Evidence Creation Lifecycle](#evidence-creation-lifecycle)
5. [Data Submission Types](#data-submission-types)
6. [Evidence Table Design](#evidence-table-design)
7. [Data Flow Diagrams](#data-flow-diagrams)
8. [Implementation Guidelines](#implementation-guidelines)
9. [Migration from Previous Design](#migration-from-previous-design)
10. [Testing Strategy](#testing-strategy)
11. [Cross-References](#cross-references)

---

## Overview

This document specifies how FaultMaven classifies and manages evidence across the investigation lifecycle. It addresses three critical design problems:

1. **Preventing duplication** between conversation messages and evidence records
2. **Optimal evidence creation timing** (after LLM processing, not before)
3. **Clear separation of concerns** between conversational data and investigative evidence

### Design Philosophy

**Core Principle**: Not all data submissions become evidence. Only data that requires analysis for the investigation should create evidence records.

**UI Constraint**: The user can upload a maximum of ONE file per turn. This simplifies the entire evidence creation workflow.

**Conversation vs. Evidence**:
- `case.messages[]` = Complete conversation history (everything the user and AI said)
- `case.evidence[]` = Only data requiring investigation analysis (files, external data, machine data)

---

## Key Design Decisions

### Decision 1: Evidence Classification at Submission

**Problem**: Previously, every data submission created an evidence record, leading to duplication between messages and evidence.

**Solution**: Classify submissions into three categories at the API layer:

| Classification | Creates Evidence | Creates Message | Example |
|----------------|------------------|-----------------|---------|
| `user_chat` | ❌ No | ✅ Yes | "Why is my app slow?" |
| `external_data` | ✅ Yes | ✅ Yes | File upload, metrics paste |
| `mixed` | ✅ Yes | ✅ Yes | "Here are my logs: [data]" |

**Rationale**:
- **user_chat**: Pure questions/responses belong only in conversation history
- **external_data**: Data requiring analysis needs both message (for continuity) and evidence (for investigation)
- **mixed**: Contains both conversation and data, extract data portion for evidence

### Decision 2: Evidence Creation Flow for File Uploads

**Problem**: File uploads were creating TWO evidence records:
1. Synthetic message evidence ("I've uploaded file.log")
2. Actual file evidence (the file content)

**Solution**: ONE evidence record per file upload following this flow:

```
1. User uploads file → API receives
2. Preprocess file → extract content
3. Build prompt with file metadata
4. LLM processes → references as "new_index_0"
5. Create ONE evidence record from LLM response
6. Return unified response
```

**Key Points**:
- Evidence record created AFTER LLM in a single phase
- File metadata passed in prompt, LLM references via `new_index_0` pattern
- Evidence created with full analysis results (no placeholder/update cycle)
- No synthetic "I uploaded X" evidence records
- One turn = One evidence record maximum (UI constraint)

### Decision 3: Evidence Table Design

**Current State**: ✅ Correct

- Evidence is case-specific child table
- No cross-case sharing capability
- Each evidence record has `case_id` foreign key

**Enhancement**: Add two unique constraints for data integrity

```sql
-- One evidence per turn maximum (UI constraint enforcement)
ALTER TABLE evidence_artifacts
ADD CONSTRAINT unique_case_turn
UNIQUE (case_id, turn_number);

-- Content-based deduplication within case
ALTER TABLE evidence_artifacts
ADD CONSTRAINT unique_case_evidence
UNIQUE (case_id, content_hash);
```

**Rationale**:

- **One turn = One evidence max**: Enforces UI constraint at database level
- **Prevent duplicate uploads**: Same file cannot be uploaded twice in same case
- **Allow same file in different cases**: Different investigative contexts
- **Content-based deduplication**: Not filename-based, catches renamed duplicates

### Decision 4: Separation of Concerns

**Clear Boundaries**:

```python
# case.messages[] - Complete conversation
messages = [
    {"role": "user", "content": "Why is my app slow?"},
    {"role": "assistant", "content": "Let me help analyze..."},
    {"role": "user", "content": "I uploaded logs"},
    {"role": "assistant", "content": "I found 127 errors..."}
]

# case.evidence[] - Only data requiring analysis
evidence = [
    {
        "evidence_id": "ev_001",
        "category": "SYMPTOM_EVIDENCE",
        "source_type": "LOG_FILE",
        "summary": "Application log: 127 errors, 45 DB timeouts",
        "content_ref": "s3://bucket/case_123/app.log"
    }
]
```

**Rules**:
- Messages capture conversational flow and context
- Evidence captures data that advances investigation
- No overlap: A submission is either pure chat OR creates evidence
- User-facing message always created (for UX continuity)

---

## Evidence Classification Flow

### Classification Decision Tree

```
                    Data Submission
                           |
                           v
              ┌────────────┴────────────┐
              |                         |
          Contains                  Pure chat
       machine data?              question/answer
              |                         |
              v                         v
         Query Classifier          user_chat
      (3-tier: hint/pattern/      (message only)
           heuristics)
              |
    ┌─────────┴─────────┐
    |                   |
    v                   v
File upload        Paste/text
 (explicit)        (implicit)
    |                   |
    v                   |
external_data          |
    |                   |
    └─────────┬─────────┘
              |
              v
    ┌─────────────────────┐
    | Contains both       |
    | conversational      |
    | AND machine data?   |
    └─────────┬───────────┘
              |
         ┌────┴────┐
         |         |
        Yes       No
         |         |
         v         v
      mixed    external_data
```

### Classification Logic

```python
def classify_submission(
    submission: str,
    is_file_upload: bool = False,
    query_type_hint: Optional[str] = None
) -> SubmissionClassification:
    """
    Classify data submission to determine evidence creation.

    Returns:
        SubmissionClassification with category and confidence
    """

    # Tier 1: Explicit file upload
    if is_file_upload:
        return SubmissionClassification(
            category="external_data",
            confidence=1.0,
            reason="explicit_file_upload"
        )

    # Tier 2: UI hint
    if query_type_hint == "machine_data":
        return SubmissionClassification(
            category="external_data",
            confidence=1.0,
            reason="explicit_hint"
        )

    # Tier 3: Pattern detection
    patterns = detect_machine_data_patterns(submission)

    if patterns.has_strong_indicators:
        # Check for mixed content (chat + data)
        has_conversational = has_conversational_markers(submission)

        if has_conversational and len(submission) > 1000:
            return SubmissionClassification(
                category="mixed",
                confidence=0.85,
                reason="chat_with_data",
                data_portion=extract_machine_data(submission)
            )
        else:
            return SubmissionClassification(
                category="external_data",
                confidence=patterns.confidence,
                reason="pattern_detection"
            )

    # Default: Pure conversational
    return SubmissionClassification(
        category="user_chat",
        confidence=0.90,
        reason="conversational_only"
    )
```

### Pattern Detection

**Machine Data Indicators**:

| Pattern Type | Regex | Weight |
|--------------|-------|--------|
| Timestamps | `\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}` | 0.3 |
| Log levels | `\b(FATAL\|ERROR\|WARN\|INFO\|DEBUG)\b` | 0.25 |
| Stack traces | `at\s+[\w.$]+\(.*?:\d+\)` | 0.35 |
| Metrics | `\d+\.\d+\s*(ms\|MB\|%\|req/s)` | 0.2 |
| JSON/YAML | `^\s*[{\[]` or `^\w+:\s*` | 0.15 |

**Scoring**:
```python
total_score = sum(weight for pattern, weight in patterns if matches(pattern))
confidence = min(0.9, 0.5 + total_score)
is_machine_data = confidence >= 0.7
```

---

## Evidence Creation Lifecycle

### Overview

Evidence creation follows a **single-phase approach** for file uploads. The evidence record is created AFTER LLM processing with complete analysis results.

### Single-Phase Creation (Post-LLM)

**When**: After LLM analyzes the file content

**Purpose**: Create evidence record with full classification and analysis

**Workflow**:

```python
# 1. File uploaded to API
file_upload = request.file

# 2. Preprocess file (extract content, compute hash)
file_content = preprocess_file(file_upload)
content_hash = compute_sha256(file_content)

# 3. Build LLM prompt with file metadata
prompt = build_prompt_with_metadata(
    file_name=file_upload.filename,
    file_size=len(file_content),
    file_type=file_upload.content_type,
    content_preview=file_content[:1000]  # First 1000 chars
)

# 4. LLM processes and references as "new_index_0"
llm_response = await llm.analyze(prompt, file_content)
# LLM response includes:
# - category: SYMPTOM_EVIDENCE, CAUSAL_EVIDENCE, or RESOLUTION_EVIDENCE
# - summary: max 500 chars
# - analysis: detailed findings
# - advances_milestones: list of milestone names
# - tests_hypothesis_id: optional hypothesis ID
# - stance: optional SUPPORTS/REFUTES/NEUTRAL
# - stance_confidence: optional 0.0-1.0

# 5. Create evidence record with complete data
evidence = Evidence(
    evidence_id=generate_evidence_id(),
    case_id=case_id,
    turn_number=case.current_turn,
    content_hash=content_hash,

    # Classification from LLM
    category=llm_response.category,
    summary=llm_response.summary,
    analysis=llm_response.analysis,

    # Storage
    content_ref=upload_file_to_storage(file_upload),
    source_type=infer_from_filename(file_upload.filename),
    form=EvidenceForm.DOCUMENT,
    content_size_bytes=len(file_content),

    # Metadata
    collected_at=datetime.now(timezone.utc),
    collected_by=current_user.email,
    collected_at_turn=case.current_turn,

    # Analysis results
    tests_hypothesis_id=llm_response.tests_hypothesis_id,
    stance=llm_response.stance,
    stance_confidence=llm_response.stance_confidence,
    advances_milestones=llm_response.advances_milestones
)

# 6. Save to database (single INSERT)
await repository.save(evidence)
```

**Key Characteristics**:

- **Single database operation**: One INSERT, no UPDATE needed
- **No placeholder phase**: Evidence created with full analysis
- **LLM references via "new_index_0"**: Pattern already exists in codebase
- **One turn = One evidence max**: UI constraint simplifies logic

### LLM Referencing Pattern

**"new_index_N" Pattern** (already exists in codebase):

```python
# LLM prompt includes file metadata (no evidence record yet)
"""
User has uploaded a file:
- Reference: new_index_0
- File: application.log (45KB)
- Type: text/plain
- Preview: [first 1000 chars of content]

Please analyze this file and provide:
1. Category (SYMPTOM_EVIDENCE, CAUSAL_EVIDENCE, or RESOLUTION_EVIDENCE)
2. Summary (max 500 chars)
3. Analysis findings
4. Which milestones this evidence advances
5. If testing hypothesis, provide hypothesis_id and stance
"""

# LLM response (structured output)
{
    "new_evidence": {
        "reference": "new_index_0",
        "category": "SYMPTOM_EVIDENCE",
        "summary": "Application log showing 127 errors...",
        "analysis": "Analysis of log patterns reveals...",
        "advances_milestones": ["symptom_verified", "timeline_established"],
        "tests_hypothesis_id": null,
        "stance": null,
        "stance_confidence": null
    }
}

# System creates evidence record from LLM response
evidence = create_evidence_from_llm_response(
    llm_response=llm_response,
    file_upload=file_upload,
    case_id=case_id,
    turn_number=case.current_turn
)
```

**UI Constraint Enforcement**:

```python
# One turn = One evidence maximum
# UI only allows single file upload per turn
# No need to handle multiple files in single request

# If user tries to upload second file in same turn:
if evidence_exists_for_turn(case_id, turn_number):
    raise ValidationError(
        "Only one file can be uploaded per turn. "
        "Please continue the conversation to upload another file."
    )
```

### Benefits of Single-Phase Approach

1. **Simpler logic** - One database INSERT, no UPDATE needed
2. **No placeholder state** - Evidence created with complete data
3. **No synthetic message evidence** - Cleaner data model
4. **Single source of truth** - One evidence record per file
5. **Leverages existing pattern** - "new_index_N" already in use
6. **UI constraint enforced** - One turn = One evidence maximum

---

## Data Submission Types

### Type 1: Pure Conversational (user_chat)

**Characteristics**:
- Natural language questions
- Follow-up queries
- Clarifications
- Confirmations

**Processing**:
```python
# Creates message only
case.messages.append({
    "role": "user",
    "content": "Why is my app slow?",
    "timestamp": datetime.now()
})

# NO evidence record created
# LLM processes as normal query
```

**Examples**:
- "Why is my app slow?"
- "Can you explain what caused this?"
- "Yes, that looks right"
- "What should I do next?"

### Type 2: External Data (external_data)

**Characteristics**:
- File uploads (explicit)
- Large log pastes
- Metrics dumps
- Configuration files
- Stack traces

**Processing**:
```python
# Phase 1: Create placeholder evidence
evidence = create_placeholder_evidence(file)

# Phase 2: LLM analyzes
analysis = await llm.analyze_evidence(evidence.content_ref)

# Phase 3: Update evidence
update_evidence_with_analysis(evidence, analysis)

# Also create message for conversational flow
case.messages.append({
    "role": "user",
    "content": f"📎 Uploaded: {file.name} ({file.size_bytes} bytes)",
    "evidence_id": evidence.evidence_id  # Link to evidence
})
```

**Examples**:
- File upload: application.log
- Paste: [500 lines of stack trace]
- Metrics: Prometheus export
- Config: database.yaml

### Type 3: Mixed Content (mixed)

**Characteristics**:
- Conversational wrapper with embedded data
- Question followed by paste
- Explanation with inline logs

**Processing**:
```python
# Extract data portion
data_portion = extract_machine_data(submission)
chat_portion = extract_conversational(submission)

# Create evidence from data portion only
evidence = create_evidence(data_portion)

# Create message with full context
case.messages.append({
    "role": "user",
    "content": submission,  # Full original text
    "evidence_id": evidence.evidence_id  # Link to extracted data
})
```

**Examples**:
- "Here are my logs: [paste 200 lines]"
- "I'm seeing errors like this: [stack trace]"
- "My config looks like: [yaml content]"

---

## Evidence Table Design

### Current Schema (With Enhancements)

```sql
CREATE TABLE evidence_artifacts (
    evidence_id VARCHAR(15) PRIMARY KEY,       -- Surrogate key (see rationale)
    case_id VARCHAR(17) NOT NULL,              -- Foreign key to cases
    user_id VARCHAR(50) NOT NULL,
    turn_number INTEGER NOT NULL,              -- When evidence was collected

    -- Classification
    category VARCHAR(50) NOT NULL,             -- SYMPTOM_EVIDENCE, CAUSAL_EVIDENCE, etc.
    source_type VARCHAR(50) NOT NULL,
    form VARCHAR(20) NOT NULL,

    -- Content
    summary TEXT NOT NULL,
    content_ref TEXT NOT NULL,                 -- S3 URI or storage path
    content_hash VARCHAR(64) NOT NULL,         -- SHA-256 for deduplication
    content_size_bytes INTEGER,

    -- Analysis (always filled in single-phase creation)
    analysis TEXT NOT NULL,
    tests_hypothesis_id VARCHAR(50),
    stance VARCHAR(20),
    stance_confidence FLOAT,

    -- Milestones
    advances_milestones JSON,                  -- Array of milestone names

    -- Metadata
    collected_at TIMESTAMP NOT NULL,
    collected_by VARCHAR(100) NOT NULL,
    collected_at_turn INTEGER NOT NULL,

    FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE,

    -- Unique constraints for data integrity
    CONSTRAINT unique_case_turn UNIQUE (case_id, turn_number),
    CONSTRAINT unique_case_evidence UNIQUE (case_id, content_hash)
);

CREATE INDEX idx_evidence_case ON evidence_artifacts(case_id);
CREATE INDEX idx_evidence_category ON evidence_artifacts(case_id, category);
CREATE INDEX idx_evidence_hypothesis ON evidence_artifacts(tests_hypothesis_id);
CREATE INDEX idx_evidence_turn ON evidence_artifacts(case_id, turn_number);
```

### Design Rationale

**Why Surrogate Key (evidence_id) Instead of Natural Key?**

While `(case_id, turn_number)` forms a viable natural composite key, we use a surrogate key for:

1. **Simpler references** - `evidence_id` easier than composite key in foreign keys
2. **API friendliness** - `/evidence/{evidence_id}` cleaner than `/evidence/{case_id}/{turn}`
3. **Future flexibility** - If turn_number becomes nullable (text-only submissions)
4. **Existing pattern** - Matches other ID patterns in codebase (`case_id`, `user_id`)

**Why Single-Phase Creation Instead of Two-Phase?**

Single-phase (create after LLM) is simpler than two-phase (placeholder before, update after):

1. **One database operation** - INSERT only, no UPDATE
2. **No intermediate state** - Evidence always complete
3. **Simpler error handling** - No orphaned placeholders
4. **Leverages existing pattern** - "new_index_N" already in codebase
5. **No race conditions** - No concurrent updates to same record

**Benefits of "One Evidence Per Turn" Constraint**

The UI constraint (one file per turn) simplifies the entire design:

1. **No batch processing** - Handle one file at a time
2. **Simpler prompts** - Always "new_index_0", never "new_index_1", "new_index_2", etc.
3. **Clearer UX** - User understands one action per turn
4. **Database enforced** - `UNIQUE (case_id, turn_number)` prevents violations
5. **Easier testing** - Fewer edge cases to handle

### Deduplication Logic

**Application Logic**:

```python
# Before creating evidence, check for duplicates
try:
    evidence = create_evidence(file, case_id, turn_number)
except IntegrityError as e:
    if "unique_case_evidence" in str(e):
        # Find existing evidence with same hash
        existing = find_evidence_by_hash(case_id, content_hash)
        return {
            "message": f"This file was already uploaded as {existing.evidence_id}",
            "evidence_id": existing.evidence_id,
            "duplicate": True
        }
    elif "unique_case_turn" in str(e):
        # Evidence already exists for this turn
        existing = find_evidence_by_turn(case_id, turn_number)
        return {
            "message": f"Evidence already exists for turn {turn_number}",
            "evidence_id": existing.evidence_id,
            "duplicate": True
        }
    raise
```

**Benefits**:

- **Content-based deduplication** - Same file cannot be uploaded twice in same case
- **Turn-based enforcement** - One evidence per turn maximum
- **Cross-case allowed** - Same file can exist in different cases (different contexts)
- **Fails fast** - Database constraint prevents invalid state

### Cross-Case Isolation

**Design Decision**: Evidence is NOT shared across cases

**Rationale**:
- Each investigation has unique context
- Same file may have different relevance in different cases
- Simplifies access control and lifecycle management
- Storage is cheap; analysis context is expensive

**Implementation**:
```python
# Evidence is always case-specific
def get_evidence(evidence_id: str, case_id: str) -> Evidence:
    """
    Retrieve evidence - must belong to specified case.
    Cannot access evidence from other cases.
    """
    evidence = db.query(Evidence).filter(
        Evidence.evidence_id == evidence_id,
        Evidence.case_id == case_id  # Enforces isolation
    ).first()

    if not evidence:
        raise NotFoundError("Evidence not found in this case")

    return evidence
```

---

## Data Flow Diagrams

### Flow 1: File Upload (external_data)

```text
┌─────────────────────────────────────────────────────────┐
│ USER ACTION                                              │
│ Uploads application.log via browser extension           │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│ API LAYER (/api/v1/cases/{case_id}/data)                │
│                                                          │
│ 1. Classify submission → external_data (file upload)    │
│ 2. Preprocess file → extract content                    │
│ 3. Compute content hash → SHA-256                       │
│ 4. Upload file to storage → S3 URI                      │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│ LLM PROCESSING                                           │
│                                                          │
│ Prompt:                                                  │
│ "User uploaded application.log (new_index_0).            │
│  File size: 45KB                                         │
│  Content preview: [first 1000 chars]                     │
│                                                          │
│  Analyze and provide:                                    │
│  - Category (SYMPTOM/CAUSAL/RESOLUTION)                  │
│  - Summary (max 500 chars)                               │
│  - Analysis findings                                     │
│  - Milestones advanced"                                  │
│                                                          │
│ LLM Response (structured output):                        │
│ {                                                        │
│   "new_evidence": {                                      │
│     "reference": "new_index_0",                          │
│     "category": "SYMPTOM_EVIDENCE",                      │
│     "summary": "Log shows 127 errors...",                │
│     "analysis": "Spike in DB timeouts at 14:23...",      │
│     "advances_milestones": ["symptom_verified"]          │
│   }                                                      │
│ }                                                        │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│ CREATE EVIDENCE (Single Phase)                          │
│                                                          │
│ INSERT INTO evidence_artifacts (                         │
│   evidence_id = 'ev_abc123',                             │
│   case_id = 'case_123',                                  │
│   turn_number = 5,                                       │
│   category = 'SYMPTOM_EVIDENCE',                         │
│   summary = 'Log shows 127 errors...',                   │
│   analysis = 'Spike in DB timeouts...',                  │
│   content_ref = 's3://bucket/case_123/app.log',          │
│   content_hash = 'a3f2c...',                             │
│   advances_milestones = '["symptom_verified"]',          │
│   collected_at = now()                                   │
│ )                                                        │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│ CREATE MESSAGE                                           │
│                                                          │
│ case.messages.append({                                   │
│   "role": "user",                                        │
│   "content": "📎 Uploaded: application.log (45KB)",      │
│   "evidence_id": "ev_abc123",                            │
│   "timestamp": now()                                     │
│ })                                                       │
│                                                          │
│ case.messages.append({                                   │
│   "role": "assistant",                                   │
│   "content": "I found 127 errors in your log...",        │
│   "sources": [{"evidence_id": "ev_abc123"}]              │
│ })                                                       │
└─────────────────────────────────────────────────────────┘
```

### Flow 2: Conversational Query (user_chat)

```text
┌─────────────────────────────────────────────────────────┐
│ USER ACTION                                              │
│ Types: "Why is my app slow?"                             │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│ API LAYER (/api/v1/cases/{case_id}/queries)             │
│                                                          │
│ 1. Classify submission → user_chat                       │
│    (no machine data patterns detected)                   │
│ 2. No evidence creation needed                           │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│ CREATE MESSAGE ONLY                                      │
│                                                          │
│ case.messages.append({                                   │
│   "role": "user",                                        │
│   "content": "Why is my app slow?",                      │
│   "timestamp": now()                                     │
│ })                                                       │
│                                                          │
│ # NO evidence record created                             │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│ LLM PROCESSING (Normal Query)                            │
│                                                          │
│ case.messages.append({                                   │
│   "role": "assistant",                                   │
│   "content": "Several factors could cause...",           │
│   "timestamp": now()                                     │
│ })                                                       │
└─────────────────────────────────────────────────────────┘
```

### Flow 3: Mixed Content (Text with Embedded Data)

```text
┌─────────────────────────────────────────────────────────┐
│ USER ACTION                                              │
│ Types: "Here are my logs: [paste 200 lines]"            │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│ API LAYER (/api/v1/cases/{case_id}/queries)             │
│                                                          │
│ 1. Classify → mixed (conversational + machine data)      │
│ 2. Extract data portion → log lines                      │
│ 3. Extract chat portion → "Here are my logs:"            │
│ 4. Compute content hash → SHA-256 of data portion        │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│ LLM PROCESSING                                           │
│                                                          │
│ Prompt:                                                  │
│ "User provided logs (new_index_0).                       │
│  Content: [200 lines of logs]                            │
│                                                          │
│  Analyze and provide:                                    │
│  - Category (SYMPTOM/CAUSAL/RESOLUTION)                  │
│  - Summary (max 500 chars)                               │
│  - Analysis findings                                     │
│  - Milestones advanced"                                  │
│                                                          │
│ LLM Response (structured output):                        │
│ {                                                        │
│   "new_evidence": {                                      │
│     "reference": "new_index_0",                          │
│     "category": "SYMPTOM_EVIDENCE",                      │
│     "summary": "Logs show memory leak...",               │
│     "analysis": "Pattern indicates...",                  │
│     "advances_milestones": ["symptom_verified"]          │
│   }                                                      │
│ }                                                        │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│ CREATE EVIDENCE (from data portion only)                 │
│                                                          │
│ INSERT INTO evidence_artifacts (                         │
│   evidence_id = 'ev_xyz',                                │
│   case_id = 'case_123',                                  │
│   turn_number = 3,                                       │
│   category = 'SYMPTOM_EVIDENCE',                         │
│   summary = 'Logs show memory leak...',                  │
│   analysis = 'Pattern indicates...',                     │
│   content_ref = 'extracted_data.txt',                    │
│   content_hash = 'b4e3d...',                             │
│   source_type = 'LOG_FILE'                               │
│ )                                                        │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│ CREATE MESSAGE (with full original content)             │
│                                                          │
│ case.messages.append({                                   │
│   "role": "user",                                        │
│   "content": "Here are my logs: [full paste]",           │
│   "evidence_id": "ev_xyz",  # Link to extracted data     │
│   "timestamp": now()                                     │
│ })                                                       │
│                                                          │
│ case.messages.append({                                   │
│   "role": "assistant",                                   │
│   "content": "I analyzed your logs...",                  │
│   "sources": [{"evidence_id": "ev_xyz"}]                 │
│ })                                                       │
└─────────────────────────────────────────────────────────┘
```

---

## Implementation Guidelines

### Backend Implementation

#### Step 1: Classification Service

```python
# services/evidence/classification_service.py

class SubmissionClassifier:
    """Classify data submissions to determine evidence creation."""

    def classify(
        self,
        content: str,
        is_file_upload: bool = False,
        query_type_hint: Optional[str] = None,
        content_type_hint: Optional[str] = None
    ) -> SubmissionClassification:
        """
        Classify submission into user_chat, external_data, or mixed.

        Tier 1: Explicit signals (file upload, UI hint)
        Tier 2: Pattern detection (timestamps, log levels, etc.)
        Tier 3: Heuristics (length, structure)
        """

        # Tier 1: Explicit file upload
        if is_file_upload:
            return SubmissionClassification(
                category="external_data",
                confidence=1.0,
                reason="explicit_file_upload"
            )

        # Tier 1: UI hint
        if query_type_hint == "machine_data":
            return SubmissionClassification(
                category="external_data",
                confidence=1.0,
                reason="ui_hint"
            )

        # Tier 2: Pattern detection
        patterns = self._detect_patterns(content)

        if patterns.confidence >= 0.7:
            # Check for mixed content
            has_chat = self._has_conversational_markers(content)

            if has_chat and len(content) > 1000:
                return SubmissionClassification(
                    category="mixed",
                    confidence=0.85,
                    reason="chat_with_data",
                    data_portion=self._extract_data_portion(content),
                    chat_portion=self._extract_chat_portion(content)
                )

            return SubmissionClassification(
                category="external_data",
                confidence=patterns.confidence,
                reason="pattern_detection",
                detected_patterns=patterns.patterns
            )

        # Default: user_chat
        return SubmissionClassification(
            category="user_chat",
            confidence=0.90,
            reason="conversational"
        )
```

#### Step 2: Evidence Service (Single-Phase)

```python
# services/evidence/evidence_service.py

class EvidenceService:
    """Manage evidence lifecycle with single-phase creation."""

    async def create_evidence_from_llm_response(
        self,
        case_id: str,
        turn_number: int,
        file: UploadFile,
        llm_analysis: LLMEvidenceAnalysis,
        user_id: str
    ) -> Evidence:
        """
        Create evidence record after LLM analysis.
        Single database INSERT with complete data.
        """

        # Preprocess file
        file_content = await file.read()
        content_hash = hashlib.sha256(file_content).hexdigest()
        file.file.seek(0)  # Reset for storage upload

        # Upload file to storage
        content_ref = await self.storage.upload(
            file=file,
            path=f"cases/{case_id}/evidence/{uuid4().hex}"
        )

        # Create evidence with complete data
        evidence = Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            case_id=case_id,
            turn_number=turn_number,

            # Classification from LLM
            category=llm_analysis.category,
            summary=llm_analysis.summary,
            analysis=llm_analysis.analysis,

            # Content
            content_ref=content_ref,
            content_hash=content_hash,
            content_size_bytes=len(file_content),
            source_type=self._infer_source_type(file.filename),
            form=EvidenceForm.DOCUMENT,

            # Metadata
            collected_at=datetime.now(timezone.utc),
            collected_by=user_id,
            collected_at_turn=turn_number,

            # Analysis results
            tests_hypothesis_id=llm_analysis.tests_hypothesis_id,
            stance=llm_analysis.stance,
            stance_confidence=llm_analysis.stance_confidence,
            advances_milestones=llm_analysis.advances_milestones
        )

        # Save to database (single INSERT)
        try:
            await self.repository.save(evidence)
        except IntegrityError as e:
            if "unique_case_evidence" in str(e):
                # Duplicate file in same case
                existing = await self.repository.find_by_hash(
                    case_id=case_id,
                    content_hash=content_hash
                )
                raise DuplicateEvidenceError(
                    f"File already uploaded as {existing.evidence_id}",
                    existing_evidence_id=existing.evidence_id
                )
            elif "unique_case_turn" in str(e):
                # Evidence already exists for this turn
                existing = await self.repository.find_by_turn(
                    case_id=case_id,
                    turn_number=turn_number
                )
                raise TurnEvidenceExistsError(
                    f"Evidence already exists for turn {turn_number}",
                    existing_evidence_id=existing.evidence_id
                )
            raise

        return evidence
```

#### Step 3: API Route Integration

```python
# api/v1/routes/data.py

@router.post("/{case_id}/data")
async def upload_case_data(
    case_id: str,
    file: UploadFile = File(...),
    session_id: str = Form(...),
    current_user: User = Depends(get_current_user),
    classifier: SubmissionClassifier = Depends(),
    evidence_service: EvidenceService = Depends(),
    llm_service: LLMService = Depends(),
    case_service: CaseService = Depends()
):
    """
    Upload data to case with evidence classification.

    Flow (Single-Phase):
    1. Classify as external_data (file upload)
    2. Preprocess file (extract content, compute hash)
    3. Build LLM prompt with file metadata
    4. LLM analyzes file (references as "new_index_0")
    5. Create evidence record from LLM response
    6. Create conversation messages
    7. Return unified response
    """

    # Get case
    case = await case_service.get(case_id)

    # Classification (always external_data for file uploads)
    classification = classifier.classify(
        content="",
        is_file_upload=True
    )

    # Preprocess file
    file_content = await file.read()
    content_hash = hashlib.sha256(file_content).hexdigest()
    file.file.seek(0)  # Reset for storage upload

    # Build LLM prompt with file metadata
    llm_prompt = f"""
    User uploaded file: {file.filename} (new_index_0)
    File size: {len(file_content)} bytes
    Content type: {file.content_type}
    Content preview: {file_content[:1000].decode('utf-8', errors='ignore')}

    Analyze this file and provide:
    1. Category: SYMPTOM_EVIDENCE, CAUSAL_EVIDENCE, or RESOLUTION_EVIDENCE
    2. Summary (max 500 characters)
    3. Key findings
    4. Milestones this evidence advances
    5. If testing hypothesis, provide hypothesis_id and stance
    """

    # LLM analyzes with "new_index_0" reference
    llm_analysis = await llm_service.analyze_evidence(
        prompt=llm_prompt,
        file_content=file_content,
        case=case
    )

    # Create evidence record (single INSERT)
    evidence = await evidence_service.create_evidence_from_llm_response(
        case_id=case_id,
        turn_number=case.current_turn,
        file=file,
        llm_analysis=llm_analysis,
        user_id=current_user.user_id
    )

    # Create conversation messages
    user_message = Message(
        role="user",
        content=f"📎 Uploaded: {file.filename} ({format_size(file.size)})",
        evidence_id=evidence.evidence_id,
        timestamp=datetime.now(timezone.utc)
    )

    ai_message = Message(
        role="assistant",
        content=llm_analysis.response_content,
        sources=[{"evidence_id": evidence.evidence_id}],
        timestamp=datetime.now(timezone.utc)
    )

    case.messages.extend([user_message, ai_message])
    await case_service.update(case)

    # Return unified response
    return DataUploadResponse(
        evidence_id=evidence.evidence_id,
        category=evidence.category,
        summary=evidence.summary,
        analysis=evidence.analysis,
        agent_response=llm_analysis.response_content,
        advances_milestones=evidence.advances_milestones
    )
```

### Frontend Integration

```typescript
// Browser extension: Handle file upload

async function handleFileUpload(file: File) {
  try {
    // Upload file
    const response = await uploadDataToCase(
      activeCaseId,
      sessionId,
      file
    );

    // Add messages to conversation (both user upload and AI analysis)
    const userMessage = {
      id: `upload-${Date.now()}`,
      question: `📎 Uploaded: ${file.name} (${formatSize(file.size)})`,
      timestamp: new Date().toISOString(),
      evidence_id: response.evidence_id
    };

    const aiMessage = {
      id: `response-${Date.now()}`,
      response: response.agent_response,
      timestamp: new Date().toISOString(),
      sources: [{
        type: "evidence",
        evidence_id: response.evidence_id
      }]
    };

    setConversation(prev => [...prev, userMessage, aiMessage]);

    // Update case progress if milestones advanced
    if (response.advances_milestones.length > 0) {
      refreshCaseProgress();
    }

  } catch (error) {
    if (error.type === "duplicate_evidence") {
      toast.info(
        `This file was already uploaded as ${error.existing_evidence_id}`
      );
    } else {
      toast.error(`Upload failed: ${error.message}`);
    }
  }
}
```

---

## Migration from Previous Design

### What Changed

#### Previous Design Issues

1. ❌ **Duplicate Evidence Records**: File uploads created both synthetic message evidence and actual file evidence
2. ❌ **Unclear Timing**: Evidence creation timing (before/after LLM) was inconsistent
3. ❌ **Message/Evidence Overlap**: Pure chat created evidence records unnecessarily
4. ❌ **No Deduplication**: Could upload same file multiple times
5. ❌ **Complex Two-Phase Logic**: Placeholder creation and updates added complexity

#### New Design Solutions

1. ✅ **Single Evidence Per File**: One evidence record per upload, no synthetic messages
2. ✅ **Single-Phase Creation**: Evidence created AFTER LLM with complete data
3. ✅ **Clear Separation**: user_chat stays in messages only, external_data creates evidence
4. ✅ **Content-Based Dedup**: Per-case unique constraint on content hash
5. ✅ **Turn-Based Enforcement**: One evidence per turn maximum (UI constraint)
6. ✅ **Simpler Logic**: One INSERT operation, no UPDATE needed

### Migration Path

#### Database Migration

```sql
-- Add turn_number column if not exists
ALTER TABLE evidence_artifacts
ADD COLUMN turn_number INTEGER;

-- Backfill turn_number from collected_at_turn
UPDATE evidence_artifacts
SET turn_number = collected_at_turn
WHERE turn_number IS NULL;

-- Make turn_number NOT NULL after backfill
ALTER TABLE evidence_artifacts
ALTER COLUMN turn_number SET NOT NULL;

-- Add content_hash column if not exists
ALTER TABLE evidence_artifacts
ADD COLUMN content_hash VARCHAR(64);

-- Backfill content_hash for existing evidence
UPDATE evidence_artifacts
SET content_hash = SHA2(content_ref, 256)
WHERE content_hash IS NULL;

-- Make content_hash NOT NULL after backfill
ALTER TABLE evidence_artifacts
ALTER COLUMN content_hash SET NOT NULL;

-- Add unique constraint for one evidence per turn
ALTER TABLE evidence_artifacts
ADD CONSTRAINT unique_case_turn
UNIQUE (case_id, turn_number);

-- Add unique constraint for content deduplication
ALTER TABLE evidence_artifacts
ADD CONSTRAINT unique_case_evidence
UNIQUE (case_id, content_hash);

-- Add index for turn-based queries
CREATE INDEX idx_evidence_turn
ON evidence_artifacts(case_id, turn_number);
```

#### Code Migration

```python
# Before (old two-phase approach - if it existed)
@router.post("/{case_id}/data")
async def upload_data_old(case_id: str, file: UploadFile):
    # Phase 1: Create placeholder
    evidence = await create_placeholder_evidence(file)

    # LLM analysis
    analysis = await llm_analyze(evidence.evidence_id, file)

    # Phase 2: Update with analysis
    evidence = await update_evidence_with_analysis(evidence, analysis)

    return evidence

# After (new single-phase approach)
@router.post("/{case_id}/data")
async def upload_data_new(case_id: str, file: UploadFile):
    # Preprocess file
    file_content = await file.read()

    # LLM analyzes with "new_index_0" reference
    analysis = await llm_analyze("new_index_0", file_content)

    # Create evidence from LLM response (single INSERT)
    evidence = await create_evidence_from_llm_response(
        file=file,
        llm_analysis=analysis,
        turn_number=case.current_turn
    )

    return evidence
```

#### Cleanup Tasks

1. **Remove synthetic message evidence**: Delete evidence records that are just "I uploaded X"
2. **Consolidate duplicate uploads**: Identify and merge duplicate file uploads in same case
3. **Backfill turn_number**: Ensure all evidence has turn_number from collected_at_turn
4. **Backfill content_hash**: Compute SHA-256 hash for existing evidence
5. **Audit message/evidence overlap**: Identify and fix cases where chat messages incorrectly created evidence
6. **Remove orphaned placeholders**: Delete any UNCLASSIFIED evidence that was never updated (if two-phase existed)

---

## Testing Strategy

### Unit Tests

#### Test 1: Classification Logic

```python
def test_file_upload_classified_as_external_data():
    """File uploads should always be external_data"""
    classifier = SubmissionClassifier()

    result = classifier.classify(
        content="",
        is_file_upload=True
    )

    assert result.category == "external_data"
    assert result.confidence == 1.0
    assert result.reason == "explicit_file_upload"

def test_pure_question_classified_as_user_chat():
    """Questions without data should be user_chat"""
    classifier = SubmissionClassifier()

    result = classifier.classify(
        content="Why is my app slow?"
    )

    assert result.category == "user_chat"
    assert result.confidence >= 0.8

def test_log_paste_classified_as_external_data():
    """Large log pastes should be external_data"""
    classifier = SubmissionClassifier()

    log_content = "\n".join([
        "2024-01-01 10:00:00 ERROR Connection timeout",
        "2024-01-01 10:00:01 ERROR Database unavailable",
        "2024-01-01 10:00:02 FATAL System crashed"
    ] * 50)

    result = classifier.classify(content=log_content)

    assert result.category == "external_data"
    assert result.confidence >= 0.7
    assert "pattern_detection" in result.reason
```

#### Test 2: Single-Phase Evidence Creation

```python
@pytest.mark.asyncio
async def test_evidence_created_after_llm():
    """Evidence created after LLM with complete data"""
    service = EvidenceService()

    # Simulate LLM analysis
    analysis = LLMEvidenceAnalysis(
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        summary="Log shows 127 errors starting 14:23 UTC",
        analysis="Database connection pool exhaustion",
        advances_milestones=["symptom_verified", "timeline_established"],
        tests_hypothesis_id=None,
        stance=None,
        stance_confidence=None
    )

    # Create evidence from LLM response
    evidence = await service.create_evidence_from_llm_response(
        case_id="case_123",
        turn_number=1,
        file=mock_file("app.log"),
        llm_analysis=analysis,
        user_id="user_1"
    )

    # Verify complete state (no placeholder, no update needed)
    assert evidence.evidence_id.startswith("ev_")
    assert evidence.category == EvidenceCategory.SYMPTOM_EVIDENCE
    assert evidence.summary == analysis.summary
    assert evidence.analysis == analysis.analysis
    assert evidence.turn_number == 1
    assert evidence.content_ref.startswith("s3://")
    assert evidence.content_hash is not None
    assert "symptom_verified" in evidence.advances_milestones

@pytest.mark.asyncio
async def test_one_evidence_per_turn_enforced():
    """Cannot create multiple evidence records for same turn"""
    service = EvidenceService()

    # Create first evidence for turn 1
    evidence1 = await service.create_evidence_from_llm_response(
        case_id="case_123",
        turn_number=1,
        file=mock_file("app.log", content="test1"),
        llm_analysis=mock_analysis(),
        user_id="user_1"
    )

    # Try creating second evidence for same turn
    with pytest.raises(TurnEvidenceExistsError) as exc:
        evidence2 = await service.create_evidence_from_llm_response(
            case_id="case_123",
            turn_number=1,  # Same turn
            file=mock_file("other.log", content="test2"),
            llm_analysis=mock_analysis(),
            user_id="user_1"
        )

    # Verify error references existing evidence
    assert exc.value.existing_evidence_id == evidence1.evidence_id
```

#### Test 3: Deduplication

```python
@pytest.mark.asyncio
async def test_duplicate_upload_rejected():
    """Uploading same file twice should fail"""
    service = EvidenceService()

    # Upload file first time
    evidence1 = await service.create_placeholder_evidence(
        case_id="case_123",
        file=mock_file("app.log", content="test content"),
        user_id="user_1",
        turn=1
    )

    # Try uploading same file again
    with pytest.raises(DuplicateEvidenceError) as exc:
        evidence2 = await service.create_placeholder_evidence(
            case_id="case_123",
            file=mock_file("app.log", content="test content"),  # Same content
            user_id="user_1",
            turn=2
        )

    # Verify error references existing evidence
    assert exc.value.existing_evidence_id == evidence1.evidence_id

@pytest.mark.asyncio
async def test_same_file_allowed_in_different_cases():
    """Same file allowed in different cases"""
    service = EvidenceService()

    file_content = "test content"

    # Upload to case 1
    evidence1 = await service.create_placeholder_evidence(
        case_id="case_1",
        file=mock_file("app.log", content=file_content),
        user_id="user_1",
        turn=1
    )

    # Upload to case 2 - should succeed
    evidence2 = await service.create_placeholder_evidence(
        case_id="case_2",
        file=mock_file("app.log", content=file_content),  # Same content
        user_id="user_1",
        turn=1
    )

    assert evidence1.evidence_id != evidence2.evidence_id
    assert evidence1.case_id != evidence2.case_id
```

### Integration Tests

```python
@pytest.mark.asyncio
async def test_file_upload_end_to_end():
    """Test complete file upload flow"""
    client = TestClient()

    # Upload file
    response = await client.post(
        "/api/v1/cases/case_123/data",
        files={"file": ("app.log", b"ERROR: test error", "text/plain")},
        data={"session_id": "sess_1"}
    )

    assert response.status_code == 201
    data = response.json()

    # Verify evidence created
    assert data["evidence_id"].startswith("ev_")
    assert data["category"] in ["SYMPTOM_EVIDENCE", "CAUSAL_EVIDENCE", "RESOLUTION_EVIDENCE"]
    assert len(data["summary"]) <= 500
    assert data["analysis"] is not None

    # Verify message created
    case = await client.get(f"/api/v1/cases/case_123")
    messages = case.json()["messages"]

    user_msg = messages[-2]
    assert user_msg["role"] == "user"
    assert "Uploaded" in user_msg["content"]
    assert user_msg["evidence_id"] == data["evidence_id"]

    ai_msg = messages[-1]
    assert ai_msg["role"] == "assistant"
    assert data["evidence_id"] in str(ai_msg["sources"])
```

---

## Cross-References

### Related Documents

- **[Data Submission Design v4.0](../data-processing/data-submission-design.md)** - API layer and user interaction
- **[Investigation Data Models](./investigation-data-models.md)** - Evidence schema and enums
- **[Data Preprocessing Architecture v2.0](../data-processing/data-preprocessing-design-specification.md)** - Data transformation pipeline
- **[Case and Session Concepts](../case-and-session/case-and-session-concepts.md)** - Case lifecycle and structure

### Design Consistency

✅ **Evidence Categories**: Uses canonical `EvidenceCategory` enum from Investigation Data Models
✅ **Source Types**: References `EvidenceSourceType` from Investigation Data Models
✅ **Classification Tiers**: Aligns with Data Submission Design 3-tier system
✅ **Preprocessing Integration**: Delegates data transformation to Preprocessing Architecture
✅ **Case Structure**: Follows Case and Session Concepts for message/evidence separation

---

## Summary

This design specification addresses the key problems in evidence classification and management:

1. **Clear Classification**: Three categories (user_chat, external_data, mixed) with explicit rules
2. **Optimal Timing**: Single-phase evidence creation AFTER LLM with complete data
3. **No Duplication**: Pure chat stays in messages; only data creates evidence
4. **Single Source of Truth**: One evidence record per file, no synthetic messages
5. **UI Constraint Enforced**: One turn = One evidence maximum (database constraint)
6. **Deduplication**: Content-based per-case constraint prevents duplicate uploads
7. **Clear Separation**: Messages for conversation, evidence for investigation
8. **Simpler Logic**: One database INSERT, no UPDATE or placeholder state needed

**Key Principle**: Not all data submissions are evidence. Only data requiring investigative analysis creates evidence records, while maintaining conversational continuity through messages for all interactions.

**Design Simplifications**:

- **UI Constraint**: User can only upload ONE file per turn (enforced at UI and database)
- **Single-Phase**: Evidence created AFTER LLM, not before (no placeholder/update cycle)
- **"new_index_N" Pattern**: LLM references via existing pattern (new_index_0 for single file)
- **Database Constraints**: Both UNIQUE(case_id, turn_number) and UNIQUE(case_id, content_hash)
- **Surrogate Key**: Keep evidence_id for simplicity despite natural key being viable
