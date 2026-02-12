# Evidence Classification - Final Design Specification

**Date:** 2026-02-11 (Updated with milestone advancement section)
**Implementation Date:** 2026-02-11
**Status:** ✅ **IMPLEMENTED** (All core phases complete, failure handling deferred)
**Context:** Complete evidence classification redesign with milestone advancement attribution

---

## Implementation Status

**✅ IMPLEMENTED:**
- Single-phase evidence creation (Phase 1-4)
- 5 evidence categories and 5 source types (Phase 1-2)
- Option 2.5 milestone attribution (Phase 4)
- Content-based classification (Phase 3-4)
- Database migration created and validated (Phase 1)
- All unit tests passing (17/17) (Phase 6)

**⏳ DEFERRED (Post-MVP):**
- Failure mode handling (Phase 7) - Design complete in [evidence-failure-modes.md](./evidence-failure-modes.md)

---

## Executive Summary

This document specifies the final agreed design for evidence classification in FaultMaven, incorporating:

1. **Single-phase evidence creation** (after LLM evaluation, not before)
2. **Classification-first approach** (LLM classifies submissions during processing)
3. **Simplified taxonomy** (clearer categories and source types)
4. **Comprehensive tracking** (all submissions tracked, including rejections)

---

## Evidence Table Semantics

**Important Clarification:**

The `evidence` table tracks two types of records:

1. **Valid Evidence** (4 categories):
   - SYMPTOM_EVIDENCE: Shows problem manifestation
   - CAUSAL_EVIDENCE: Points to root cause
   - RESOLUTION_EVIDENCE: Validates fix effectiveness
   - CONTEXTUAL_EVIDENCE: Provides baseline/environmental context

2. **Rejected Submissions** (1 category):
   - REJECTED: Analyzed but determined not useful for investigation

**Why track rejected submissions?**

- **Deduplication**: Prevent re-uploading same file (via `content_hash`)
- **Audit trail**: Complete record of what was submitted and evaluated
- **Cost efficiency**: Avoid re-analyzing rejected files with LLM
- **User feedback**: Explain to user why submission was rejected
- **Flexibility**: Can "un-reject" if investigation context changes

**Terminology Note**: The table is called `evidence` for historical and pragmatic reasons, but conceptually represents "analyzed submissions" (both accepted and rejected). **Rejected submissions are NOT evidence** - they exist in this table for practical tracking purposes only.

**Querying valid evidence only:**
```python
# In Python
valid_evidence = [e for e in case.evidence if e.category != EvidenceCategory.REJECTED]

# Or use helper method
valid_evidence = case.valid_evidence
```

```sql
-- In SQL
SELECT * FROM evidence WHERE case_id = ? AND category != 'rejected';
```

---

## Core Design Decisions

### Decision 1: Evidence Table Includes Rejected Submissions

**Design Choice:** Evidence table tracks ALL file upload attempts, including those classified as rejected.

**Rationale:**

- **Deduplication**: Prevent re-uploading same rejected file (via `content_hash`)
- **Audit trail**: Complete record of what was submitted and evaluated
- **Cost efficiency**: Avoid re-analyzing rejected files with LLM
- **User feedback**: Explain to user why submission was rejected
- **Flexibility**: Can "un-reject" if investigation context changes

**Implementation:** Add `REJECTED` category to track rejected submissions.

**Semantic Note:** The table is called `evidence` for historical reasons, but conceptually represents "analyzed submissions" (both accepted and rejected). Rejected submissions are NOT evidence, but are tracked here for practical reasons listed above.

---

### Decision 2: Single-Phase Evidence Creation

**Design Choice:** Evidence records created AFTER LLM evaluation, not before.

**Previous Flow (Deprecated):**
```
User submits → Create Evidence(UNCLASSIFIED) → LLM sees → LLM promotes to category
```

**New Flow (Approved):**
```
User submits → LLM evaluates → If relevant: Create Evidence(category)
                              → If rejected: Create Evidence(REJECTED)
                              → If pure chat: No evidence record
```

**Rationale:**
- Eliminates UNCLASSIFIED placeholder pattern
- LLM can still reference evidence using "new_index_N" pattern (already supported)
- Simpler lifecycle (create once with complete data)
- Aligns with "evidence table = submission attempts" mental model

---

### Decision 3: Classification Types

**Three submission types:**
```python
class SubmissionClassification(str, Enum):
    USER_CHAT = "user_chat"           # Pure conversation → NO evidence record
    EXTERNAL_DATA = "external_data"   # Data from elsewhere → Evidence record
    MIXED = "mixed"                   # Chat + data → Evidence record (extract data portion)
```

**Handling:**
- `USER_CHAT`: Stays in `case.messages[]` only, never enters `evidence` table
- `EXTERNAL_DATA`: Always creates evidence record
- `MIXED`: Creates evidence record for data portion, chat stays in messages

---

### Decision 4: Evidence Categories (Refined)

**Previous (5 categories):**
- UNCLASSIFIED, SYMPTOM_EVIDENCE, CAUSAL_EVIDENCE, RESOLUTION_EVIDENCE, OTHER

**New (5 categories):**

```python
class EvidenceCategory(str, Enum):
    """Evidence classification by investigation purpose"""

    # ===== RELEVANT EVIDENCE (4 categories) =====

    SYMPTOM_EVIDENCE = "symptom_evidence"
    """
    Shows problem manifestation.

    Purpose: Prove the problem exists and establish scope/timeline.

    IMPORTANT: This category describes what the data CONTAINS, not whether the user
    has committed to investigating. A log file with errors is SYMPTOM_EVIDENCE even
    during INQUIRY phase (exploratory upload before problem confirmed).

    Examples:
    - Error logs showing failures
    - Metrics showing degradation (high CPU, slow response times)
    - User impact reports
    - Deployment logs showing recent changes

    Advances Milestones: symptom_verified, scope_assessed, timeline_established
    (Note: Milestone validation only runs during INVESTIGATING status. Evidence
    created during INQUIRY sits inert until investigation begins.)
    """

    CAUSAL_EVIDENCE = "causal_evidence"
    """
    Points to root cause.

    Purpose: Test hypothesis about what caused the problem.

    Examples:
    - Connection pool metrics (for "pool exhausted" hypothesis)
    - Memory dumps (for "memory leak" hypothesis)
    - Network traces (for "latency" hypothesis)
    - Config changes (for "misconfiguration" hypothesis)

    Advances Milestones: root_cause_identified
    """

    RESOLUTION_EVIDENCE = "resolution_evidence"
    """
    Validates fix effectiveness.

    Purpose: Prove that solution resolved the problem.

    Examples:
    - Error rate after rollback (before/after comparison)
    - Latency metrics after optimization
    - Resource usage after scaling
    - Success rate after config change

    Advances Milestones: solution_verified
    """

    CONTEXTUAL_EVIDENCE = "contextual_evidence"  # Was: OTHER
    """
    Provides baseline, environmental, or background context.

    Purpose: Help understand system or problem context without directly
    showing symptoms, proving causes, or validating resolutions.

    Characteristics:
    - Describes "what is already there" (baseline, normal state)
    - Neither problematic nor a fix
    - System configuration, architecture, or operational context
    - Historical baseline or reference data

    Examples:
    - System architecture diagrams
    - Current/baseline configuration files
    - "Normal" resource usage patterns (for comparison)
    - System inventory (versions, dependencies, infrastructure)
    - SLA requirements or business context
    - Historical incident reports (for reference)

    INQUIRY Phase Usage:
    - If uploaded data truly shows NO problems (clean logs, normal metrics),
      classify as CONTEXTUAL_EVIDENCE
    - If data shows problems (errors, anomalies), classify as SYMPTOM_EVIDENCE
      even during INQUIRY phase (classify based on content, not user's commitment)

    Does NOT directly advance milestones, but helps LLM understand environment.
    """

    # ===== REJECTED SUBMISSIONS =====

    REJECTED = "rejected"
    """
    Submission analyzed but rejected as not useful for investigation.

    IMPORTANT: This is NOT evidence. It exists in the evidence table for
    practical reasons (deduplication, audit trail, cost avoidance), not
    because it's evidence.

    Purpose: Track rejected submissions for:
    - Deduplication (prevent re-upload via content_hash)
    - Audit trail (what was submitted and why rejected)
    - Cost avoidance (don't re-analyze same file)
    - User feedback (explain why rejected)

    Can be "un-rejected" if investigation context changes.

    Examples:
    - Screenshots unrelated to issue
    - Logs from unrelated services
    - Accidental uploads
    - Files determined not useful after analysis

    Reasoning captured in `primary_purpose` field.

    Note: Duplicate files are also marked as REJECTED with reference to original.
    """
```

**Key Changes:**

1. ~~UNCLASSIFIED~~ → **REMOVED** (no longer needed with single-phase creation)
2. OTHER → **CONTEXTUAL_EVIDENCE** (clearer purpose: baseline/context data)
3. **REJECTED** → **ADDED** (track rejected submissions for deduplication and audit)

**Classification Guidance for LLM:**
```
When classifying evidence, ask:

1. Does it show the PROBLEM happening? → SYMPTOM_EVIDENCE
2. Does it point to the ROOT CAUSE? → CAUSAL_EVIDENCE
3. Does it prove the FIX worked? → RESOLUTION_EVIDENCE
4. Does it provide CONTEXT/BASELINE (but not problem/cause/fix)? → CONTEXTUAL_EVIDENCE
5. Is it unrelated to this case? → REJECTED

CRITICAL: Classify based on what the DATA CONTAINS, not the investigation phase.
- Log file with errors = SYMPTOM_EVIDENCE (even in INQUIRY phase)
- Clean logs with no issues = CONTEXTUAL_EVIDENCE (even in INQUIRY phase)
- Don't wait for investigation confirmation to classify symptom data
```

---

### Decision 5: Evidence Source Types (Simplified)

**Previous (12 types):**
- LOG_FILE, COMMAND_OUTPUT, TRACE_DATA, API_RESPONSE, METRICS_DATA, MONITORING_ALERT, CONFIG_FILE, CODE_REVIEW, DATABASE_QUERY, SCREENSHOT, USER_REPORT, OTHER

**New (5 types):**

```python
class EvidenceSourceType(str, Enum):
    """Fundamental type of data source"""

    LOGS = "logs"
    """
    Any textual diagnostic output.

    Includes:
    - Application logs
    - System logs
    - Command output (kubectl, curl, docker logs, etc.)
    - Distributed trace data
    - API responses
    - Error messages

    Characteristics: Time-ordered textual records of system behavior
    """

    METRICS = "metrics"
    """
    Quantitative measurements.

    Includes:
    - Time-series metrics (CPU, memory, latency)
    - Dashboards and graphs
    - Performance data
    - Resource usage statistics
    - Monitoring alerts (triggered by metrics)

    Characteristics: Numerical data, often time-series
    """

    CONFIGURATION = "configuration"
    """
    System/application configuration.

    Includes:
    - Config files (YAML, JSON, TOML, env vars)
    - Code snippets
    - Database schema
    - Infrastructure definitions (Kubernetes manifests, Terraform)
    - Dependency lists

    Characteristics: Defines how system should behave
    """

    VISUAL = "visual"
    """
    Visual representations.

    Includes:
    - Screenshots (errors, dashboards, terminals)
    - Architecture diagrams
    - Graphs and charts
    - Images

    Characteristics: Requires visual interpretation
    """

    USER_DESCRIPTION = "user_description"
    """
    User's typed narrative.

    Includes:
    - Problem descriptions
    - Observations
    - Impact reports
    - Steps to reproduce
    - Context explanations

    Characteristics: Human-written context, not machine-generated data
    """
```

**Migration Mapping:**
| Old (12 types) | New (5 types) |
|----------------|---------------|
| LOG_FILE | LOGS |
| COMMAND_OUTPUT | LOGS |
| TRACE_DATA | LOGS |
| API_RESPONSE | LOGS |
| METRICS_DATA | METRICS |
| MONITORING_ALERT | METRICS |
| CONFIG_FILE | CONFIGURATION |
| CODE_REVIEW | CONFIGURATION |
| DATABASE_QUERY | CONFIGURATION (schema) or LOGS (results) |
| SCREENSHOT | VISUAL |
| USER_REPORT | USER_DESCRIPTION |
| OTHER | LOGS (fallback) |

**Rationale for Simplification:**
1. **Easier for LLM to classify** - 5 clear choices vs 12 overlapping ones
2. **Clear distinctions** - Each type has unique characteristics
3. **Reduces ambiguity** - "LOG_FILE vs COMMAND_OUTPUT?" → Just LOGS
4. **Still captures value** - Preprocessing can provide detailed `data_type` if needed

**Note:** The `data_type` field from preprocessing can still provide granular detail:
- `source_type = LOGS`, `data_type = "application_log"`
- `source_type = LOGS`, `data_type = "command_output"`
- `source_type = LOGS`, `data_type = "distributed_trace"`

---

## INQUIRY Phase Classification (First-Class Scenario)

### Principle: Classify Based on Content, Not Investigation Phase

**Common Scenario:**

```text
Turn 1 (INQUIRY phase):
User: "I've got this log file, can you take a look and tell me what's wrong?"
*uploads application.log*
```

**Question:** How should this be classified when the user hasn't committed to investigating yet?

**Answer:** **Classify based on what the data contains**, not the user's investigation commitment.

### Classification During INQUIRY Phase

```python
# Log file WITH errors/anomalies → SYMPTOM_EVIDENCE
if log_shows_errors or log_shows_anomalies:
    category = EvidenceCategory.SYMPTOM_EVIDENCE
    # Rationale: The data contains problem manifestation

# Log file WITHOUT errors (clean, normal) → CONTEXTUAL_EVIDENCE
elif log_is_clean and log_shows_normal_operation:
    category = EvidenceCategory.CONTEXTUAL_EVIDENCE
    # Rationale: The data provides baseline/context

# Unrelated or accidental upload → REJECTED
elif not_useful_for_investigation:
    category = EvidenceCategory.REJECTED
```

### Key Insight

**The category describes what the data contains, not whether the user has decided to act on it.**

- A log file with errors is **SYMPTOM_EVIDENCE** regardless of investigation phase
- A config file showing normal settings is **CONTEXTUAL_EVIDENCE** during INQUIRY
- If later analysis shows data is unrelated, it can be marked **REJECTED**

### How Milestone Advancement Works

**During INQUIRY Phase:**
- Evidence is classified normally (SYMPTOM, CONTEXTUAL, etc.)
- `validate_milestone_claims()` does **NOT run** (only runs during INVESTIGATING status)
- Evidence **sits inert** until investigation begins
- No milestones are advanced during INQUIRY

**When Investigation Begins (INQUIRY → INVESTIGATING):**
- Existing evidence is already classified
- Milestone engine starts processing milestone claims
- Previously uploaded evidence contributes to milestone advancement

### Example Scenario

```text
Turn 1 (INQUIRY):
User: "Can you check this log file?"
*uploads app.log with connection timeout errors*

LLM Classification:
- submission_classification: "external_data"
- category: SYMPTOM_EVIDENCE  ← Classified based on content
- summary: "Application logs showing database connection timeouts"
- primary_purpose: "Shows repeated connection timeout errors during peak hours"

Evidence Created:
- evidence_id: ev_abc123
- category: SYMPTOM_EVIDENCE
- collected_at_turn: 1
- advances_milestones: []  ← Empty during INQUIRY (no milestone validation)

Turn 2 (INQUIRY):
User: "This looks bad, let's investigate"

Status Changes: INQUIRY → INVESTIGATING

Turn 3 (INVESTIGATING):
User uploads additional evidence showing connection pool exhausted

LLM Processing:
- MilestoneUpdates: {symptom_verified: true, scope_assessed: true}
- System infers: ev_abc123.advances_milestones = ["symptom_verified", "scope_assessed"]
  (Evidence from turn 1 now contributes to milestones completed in turn 3)
```

### Implementation Notes

1. **No special handling needed** - Classification logic is the same regardless of phase
2. **Milestone validation guarded** - Already implemented in milestone_engine.py (only runs during INVESTIGATING)
3. **Evidence retroactively contributes** - When milestones complete, system infers which evidence (including INQUIRY-phase evidence) contributed
4. **LLM guidance critical** - Prompt templates must emphasize "classify based on content, not phase"

### Prompt Template Guidance

```text
EVIDENCE CLASSIFICATION DURING INQUIRY PHASE:

Classify evidence based on WHAT THE DATA CONTAINS, not the investigation phase:

- Log file with errors → SYMPTOM_EVIDENCE (even if user hasn't committed to investigating)
- Metrics showing anomalies → SYMPTOM_EVIDENCE (classify problems immediately)
- Clean logs/configs → CONTEXTUAL_EVIDENCE (provides baseline)
- Unrelated data → REJECTED

Do NOT wait for investigation confirmation to classify symptom evidence.
The category reflects data content, not user's investigation commitment.
```

---

## Complete Evidence Schema

```python
class Evidence(BaseModel):
    """
    Investigation evidence and analyzed submissions.

    IMPORTANT SEMANTIC NOTE:
    This model tracks both:
    1. Valid evidence (SYMPTOM, CAUSAL, RESOLUTION, CONTEXTUAL)
    2. Rejected submissions (REJECTED category)

    Rejected submissions are NOT evidence, but are tracked here for:
    - Deduplication: Prevent re-uploading same rejected file
    - Audit trail: Record what was submitted and evaluated
    - Cost efficiency: Avoid re-analyzing rejected submissions
    - User feedback: Explain why submission was rejected

    To query only valid evidence:
        evidence = [e for e in case.evidence if e.category != EvidenceCategory.REJECTED]

    Or in SQL:
        SELECT * FROM evidence WHERE category != 'rejected';
    """

    # Identity
    evidence_id: str = Field(pattern=r"^ev_[a-f0-9]{12}$")
    case_id: str = Field(pattern=r"^case_[a-f0-9]{12}$")

    # Classification (NEW: assigned by LLM at creation time)
    category: EvidenceCategory  # SYMPTOM/CAUSAL/RESOLUTION/CONTEXTUAL/REJECTED
    source_type: EvidenceSourceType  # LOGS/METRICS/CONFIGURATION/VISUAL/USER_DESCRIPTION
    form: EvidenceForm  # DOCUMENT (file upload) or USER_INPUT (typed text)

    # Content
    summary: str = Field(max_length=500)
    primary_purpose: str  # What this evidence shows, or why rejected if REJECTED
    content_ref: str  # S3 URI, file_id, or turn reference
    preprocessed_content: Optional[str] = None  # Extracted text (for search)

    # Metadata
    collected_at: datetime
    collected_by: str  # user_id
    collected_at_turn: int

    # Processing
    preprocessing_method: str  # "none", "ocr", "log_parser", etc.
    content_size_bytes: int
    content_hash: Optional[str] = None  # SHA256 for deduplication

    # Investigation linkage
    related_hypotheses: List[str] = []  # hypothesis_ids this evidence evaluates
    advances_milestones: List[str] = []  # milestones this evidence advances


class Case(BaseModel):
    """Case with helper methods for evidence filtering"""

    evidence: List[Evidence] = []

    @property
    def valid_evidence(self) -> List[Evidence]:
        """Return only valid evidence (excludes rejected submissions)"""
        return [e for e in self.evidence if e.category != EvidenceCategory.REJECTED]

    @property
    def rejected_submissions(self) -> List[Evidence]:
        """Return only rejected submissions"""
        return [e for e in self.evidence if e.category == EvidenceCategory.REJECTED]

    @property
    def acceptance_rate(self) -> float:
        """Calculate evidence acceptance rate (%)"""
        if not self.evidence:
            return 0.0
        valid_count = len(self.valid_evidence)
        return (valid_count / len(self.evidence)) * 100.0
```

---

## Database Constraints

```sql
CREATE TABLE evidence (
    evidence_id VARCHAR(17) PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- Classification
    category VARCHAR(50) NOT NULL,  -- New values: contextual_evidence, rejected
    source_type VARCHAR(50) NOT NULL,  -- New values: logs, metrics, configuration, visual, user_description
    form VARCHAR(20) NOT NULL,

    -- Content
    summary TEXT NOT NULL,
    primary_purpose TEXT NOT NULL,
    content_ref VARCHAR(500) NOT NULL,
    preprocessed_content TEXT,

    -- Metadata
    collected_at TIMESTAMP WITH TIME ZONE NOT NULL,
    collected_by VARCHAR(36) NOT NULL,
    collected_at_turn INTEGER NOT NULL,

    -- Processing
    preprocessing_method VARCHAR(50) NOT NULL,
    content_size_bytes INTEGER NOT NULL,
    content_hash VARCHAR(64),  -- SHA256

    -- Constraints
    UNIQUE (case_id, turn_number),  -- UI constraint: one evidence per turn max
    UNIQUE (case_id, content_hash)  -- Prevent duplicate uploads to same case
);

CREATE INDEX idx_evidence_case_category ON evidence(case_id, category);
CREATE INDEX idx_evidence_case_turn ON evidence(case_id, collected_at_turn);
CREATE INDEX idx_evidence_hash ON evidence(case_id, content_hash) WHERE content_hash IS NOT NULL;
```

---

## Analytics Queries Enabled

### Total Submissions vs Accepted Evidence
```sql
-- Total file submissions
SELECT COUNT(*) as total_submissions
FROM evidence
WHERE case_id = ?;

-- Relevant evidence only (4 categories)
SELECT COUNT(*) as relevant_evidence
FROM evidence
WHERE case_id = ?
  AND category IN ('symptom_evidence', 'causal_evidence', 'resolution_evidence', 'contextual_evidence');

-- Acceptance rate
SELECT
    COUNT(*) as total,
    COUNT(CASE WHEN category != 'rejected' THEN 1 END) as accepted,
    ROUND(COUNT(CASE WHEN category != 'rejected' THEN 1 END) * 100.0 / COUNT(*), 2) as acceptance_rate
FROM evidence
WHERE case_id = ?;
```

### Rejection Analysis
```sql
-- Why were submissions rejected?
SELECT
    primary_purpose as rejection_reason,
    COUNT(*) as count
FROM evidence
WHERE case_id = ? AND category = 'rejected'
GROUP BY primary_purpose
ORDER BY count DESC;
```

### Evidence Breakdown by Category
```sql
SELECT
    category,
    COUNT(*) as count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 2) as percentage
FROM evidence
WHERE case_id = ?
GROUP BY category
ORDER BY count DESC;
```

---

## Implementation Flow

### User Submits File Upload

```
1. User uploads file via /api/v1/cases/{case_id}/data
   ↓
2. File preprocessing:
   - Extract text/metadata
   - Generate content_hash (SHA256)
   - Store in S3/local storage
   ↓
3. Create synthetic message in case.messages[]
   - role: "user"
   - content: "I've uploaded {filename}"
   - attachments: [file_metadata]
   ↓
4. Call investigation_service.process_turn()
   ↓
5. LLM receives:
   - Full case context (existing evidence, hypotheses)
   - User message with attachment metadata
   - File content (preprocessed text)
   ↓
6. LLM returns structured response with:
   - submission_classification: {type: "external_data", reasoning: "..."}
   - state_updates.evidence_to_add: [{
       category: "symptom_evidence",  # or "rejected" if not useful
       source_type: "logs",
       summary: "Database connection timeout errors",
       primary_purpose: "Shows connection pool exhaustion at peak hours"
     }]
   ↓
7. Create Evidence record:
   - evidence_id: ev_{uuid}
   - category: From LLM
   - collected_at_turn: current_turn
   - content_hash: From preprocessing
   ↓
8. Check deduplication:
   - If content_hash exists for this case_id:
     - Update primary_purpose: "Duplicate of evidence from turn X"
     - Skip milestone advancement
   ↓
9. Save to database
```

### User Submits Pure Chat

```
1. User sends message via /api/v1/cases/{case_id}/queries
   ↓
2. Save message to case.messages[]
   ↓
3. Call investigation_service.process_turn()
   ↓
4. LLM receives:
   - Full case context
   - User message text only (no attachments)
   ↓
5. LLM returns:
   - submission_classification: {type: "user_chat"}
   - agent_response: "..."
   - NO evidence_to_add
   ↓
6. NO Evidence record created
   ↓
7. Return response to user
```

---

## Milestone Advancement Attribution

### Design Decision: Hybrid System-Inferred with Optional LLM Override (Option 2.5)

**Context:** Evidence records have an `advances_milestones` field to track which investigation milestones each piece of evidence helped complete. This provides granular attribution for analytics and forensics.

**Problem:** Should this field be:
1. Removed entirely (no granular tracking)
2. System-inferred (automatic attribution)
3. LLM-specified (explicit attribution)
4. Hybrid (system infers by default, LLM can override)

**Solution: Option 2.5 - Three-Tier Logic**

```text
1. MilestoneUpdates drives state (turn-level, LLM specifies) → UNCHANGED
2. System infers advances_milestones from category (NEW — handles 90%)
3. LLM overrides when explicit (NEW — handles 10%)
```

### Category-Milestone Mapping

The system infers which milestones an evidence record advanced based on its category:

```python
CATEGORY_MILESTONE_MAP = {
    EvidenceCategory.SYMPTOM_EVIDENCE: [
        "symptom_verified",
        "scope_assessed",
        "timeline_established",
        "changes_identified",
    ],
    EvidenceCategory.CAUSAL_EVIDENCE: [
        "changes_identified",
        "root_cause_identified",
        "solution_proposed",
    ],
    EvidenceCategory.RESOLUTION_EVIDENCE: [
        "solution_applied",
    ],
    EvidenceCategory.CONTEXTUAL_EVIDENCE: [
        # Provides supporting context but doesn't directly advance milestones
    ],
    EvidenceCategory.REJECTED: [
        # Not evidence, no milestones
    ],
}
```

### Inference Logic

```python
def _infer_milestones(
    category: EvidenceCategory,
    milestones_completed_this_turn: List[str]
) -> List[str]:
    """
    Infer which milestones this evidence advanced.

    Returns intersection of:
    - Milestones this category can advance (CATEGORY_MILESTONE_MAP)
    - Milestones actually completed this turn (from MilestoneUpdates)
    """
    eligible = CATEGORY_MILESTONE_MAP.get(category, [])
    return [m for m in milestones_completed_this_turn if m in eligible]
```

### Example

```text
Turn 5:
- User uploads log file showing error traces
- LLM evaluates, creates SYMPTOM_EVIDENCE
- LLM sets MilestoneUpdates: {symptom_verified: true, scope_assessed: true}
- System infers: advances_milestones = ["symptom_verified", "scope_assessed"]
  (intersection of eligible milestones for SYMPTOM_EVIDENCE and completed this turn)
```

### Optional LLM Override

The LLM can optionally specify `advances_milestones` explicitly when system inference would be incorrect (~10% of cases):

```python
class EvidenceToAdd(BaseModel):
    summary: str
    category: EvidenceCategory
    source_type: EvidenceSourceType

    # OPTIONAL: Override system inference
    advances_milestones: Optional[List[str]] = Field(
        None,
        description=(
            "OPTIONAL: Explicitly specify which milestones this evidence advances. "
            "If not provided, system will infer based on category. "
            "Only specify when inference would be incorrect (rare: ~10% of cases)."
        )
    )
```

### When LLM Should Override

- Evidence doesn't contribute to usual milestones for its category
- Evidence advances a milestone outside its typical category
- Need more precise attribution than category allows

### Benefits

#### Compared to removing the field

- ✅ Preserves traceability ("Which evidence led to symptom_verified?")
- ✅ Enables analytics (evidence impact, attribution metrics)
- ✅ Supports forensic review ("How did we conclude X?")

#### Compared to always requiring LLM specification

- ✅ Zero token cost for common cases (90%)
- ✅ No LLM cognitive load for obvious mappings
- ✅ No risk of inconsistency (inference is deterministic)

#### Compared to pure system inference

- ✅ Handles edge cases where inference would be wrong
- ✅ LLM can be more precise when needed

### Implementation Notes

1. **Single source of truth**: MilestoneUpdates (turn-level) drives `case.progress` state
2. **Derived attribute**: `advances_milestones` (evidence-level) is inferred from category + completed milestones
3. **No inconsistency risk**: System controls both values with clear derivation logic
4. **One-file-per-turn constraint**: UI limitation makes inference unambiguous (only one evidence to attribute to)

The milestone advancement design uses a hybrid system-inferred approach with optional LLM override (Option 2.5).

---

## Migration Strategy

### Phase 1: Schema Updates
1. Add new enum values:
   - `EvidenceCategory.CONTEXTUAL_EVIDENCE`
   - `EvidenceCategory.REJECTED`
   - New `EvidenceSourceType` values (LOGS, METRICS, etc.)
2. Create migration to rename/map existing values
3. Add database constraints (UNIQUE on turn_number, content_hash)

### Phase 2: Code Updates
1. Remove UNCLASSIFIED evidence creation pattern (milestone_engine.py:359-400)
2. Add submission_classification to LLM response schemas
3. Update prompt templates with classification guidance
4. Implement single-phase evidence creation logic
5. Add content_hash deduplication check

### Phase 3: Testing
1. Unit tests for classification logic
2. Integration tests for evidence creation flow
3. Test deduplication (same file uploaded twice)
4. Test analytics queries

### Phase 4: Rollout
1. Deploy schema changes
2. Migrate existing UNCLASSIFIED → appropriate category or REJECTED
3. Migrate existing OTHER → CONTEXTUAL_EVIDENCE
4. Deploy code changes
5. Monitor evidence creation patterns

---

## Open Questions

None - design is finalized and approved.

---

## Appendix: Design Evolution

### What Changed from Original Design

1. **UNCLASSIFIED removed**: Was placeholder pattern, now single-phase creation
2. **OTHER renamed**: Now CONTEXTUAL_EVIDENCE with clear definition
3. **REJECTED added**: Track rejected submissions for deduplication and audit
4. **Source types simplified**: 12 → 5 types for clarity
5. **Evidence table semantics**: Now "submission attempts table" not "validated evidence only"

### Why These Changes

- **Simpler mental model**: One creation, complete data
- **Better analytics**: Track all submissions including rejections
- **Clearer taxonomy**: CONTEXTUAL_EVIDENCE vs vague OTHER
- **Easier classification**: 5 source types vs 12 overlapping ones
- **Complete audit trail**: Can answer "what was submitted?" and "why rejected?"

---

**Document Status:** Approved for Implementation
**Next Step:** Create detailed implementation plan with task breakdown
