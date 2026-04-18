# Evidence Classification Design Specification

**Version:** 2.2
**Date:** 2026-02-23
**Status:** Design Specification
**Context:** Evidence classification taxonomy, single-phase creation, and milestone advancement attribution

---

## Executive Summary

This document specifies the design for evidence classification in FaultMaven:

1. **Single-phase evidence creation** — evidence records created after LLM evaluation, not before
2. **Payload-driven form determination** — evidence form set by ingestion pipeline (attachments -> DOCUMENT, agent findings -> SUBMITTED_DATA), not by LLM classification
3. **Unified DataType taxonomy** — 6 types shared with preprocessing ([Data Preprocessing](./data-preprocessing-design-specification.md))
4. **5 evidence categories** — SYMPTOM, CAUSAL, RESOLUTION, CONTEXTUAL, REJECTED
5. **Comprehensive tracking** — all submissions tracked, including rejections

---

## Evidence Table Semantics

**Important Clarification:**

The `evidence` table tracks two types of records:

1. **Valid Evidence** (5 categories):
   - SYMPTOM_EVIDENCE: Shows problem manifestation
   - CAUSAL_EVIDENCE: Points to root cause
   - MITIGATION_EVIDENCE: Validates that a temporary mitigation reduced impact
   - SOLUTION_EVIDENCE: Validates that the permanent fix resolved the root cause
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

```
User submits → LLM evaluates → If relevant: Create Evidence(category)
                              → If rejected: Create Evidence(REJECTED)
                              → If pure chat: No evidence record
```

**Rationale:**
- No placeholder pattern needed — create once with complete data
- LLM can reference evidence using "new_index_N" pattern
- Simpler lifecycle (single creation, no promotion)
- Aligns with "evidence table = submission attempts" mental model

---

### Decision 3: Payload-Driven Evidence Form (supersedes `SubmissionClassification`)

> **Supersedes (2026-02-22)**: The older `SubmissionClassification` enum (USER_TEXT / SUBMITTED_DATA / MIXED) was removed as part of the Unified Ingestion Pipeline (v4.1). Evidence form is now determined by payload context, not by an LLM classifier. See [Data Preprocessing Design Specification](./data-preprocessing-design-specification.md) §2.4 and §8.

**Current behavior (Unified Ingestion Pipeline):**

- Turn has attachments → `EvidenceForm.DOCUMENT` (set by `_preprocess_attachment()`)
- Query-only turn → `EvidenceForm.USER_TEXT` (default; typically no evidence is created)
- Agent tool output → `EvidenceForm.SUBMITTED_DATA` (set by the milestone engine when applying `evidence_to_add`)

---

### Decision 4: Evidence Categories

**6 categories:**

```python
class EvidenceCategory(str, Enum):
    """Evidence classification by investigation purpose"""

    # ===== RELEVANT EVIDENCE (5 categories) =====

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

    MITIGATION_EVIDENCE = "mitigation_evidence"
    """
    Validates that a temporary mitigation reduced or contained impact while
    the root cause is still being investigated or resolved.

    Purpose: Prove the mitigation is holding (the bleeding stopped) without
    yet claiming root-cause resolution.

    Examples:
    - Error rate after a feature-flag rollback
    - Saturation drop after auto-scaling kicked in
    - Latency restored after a traffic shift to a healthy region

    Advances Milestones: mitigation_applied
    """

    SOLUTION_EVIDENCE = "solution_evidence"
    """
    Validates that the permanent fix resolved the problem at its root cause.

    Purpose: Prove the solution worked end-to-end after the root cause was
    addressed, not just that symptoms were temporarily masked.

    Examples:
    - Error rate after the fix shipped (before/after comparison)
    - Latency metrics after the underlying optimization
    - Resource usage after the corrected configuration is in place

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

**Classification Guidance for LLM:**
```
When classifying evidence, ask:

1. Does it show the PROBLEM happening? → SYMPTOM_EVIDENCE
2. Does it point to the ROOT CAUSE? → CAUSAL_EVIDENCE
3. Does it prove a TEMPORARY MITIGATION held (root cause still open)? → MITIGATION_EVIDENCE
4. Does it prove the PERMANENT FIX worked at the root cause? → SOLUTION_EVIDENCE
5. Does it provide CONTEXT/BASELINE (but not problem/cause/fix)? → CONTEXTUAL_EVIDENCE
6. Is it unrelated to this case? → REJECTED

CRITICAL: Classify based on what the DATA CONTAINS, not the investigation phase.
- Log file with errors = SYMPTOM_EVIDENCE (even in INQUIRY phase)
- Clean logs with no issues = CONTEXTUAL_EVIDENCE (even in INQUIRY phase)
- Don't wait for investigation confirmation to classify symptom data
```

### Category Stability

**Design Rule:** The `category` field is **stable after initial assignment**. Once the LLM classifies a submission, that classification should not change under normal operation.

**Rationale:**
- Milestone attribution (`advances_milestones`) is inferred at creation time from the category; changing the category retroactively would invalidate attribution history
- The `content_hash` deduplication constraint relies on stable category assignments — re-categorizing could create semantic duplicates
- Investigation audit trails require a stable classification record

**Exception — Un-rejecting:** As noted in the `REJECTED` category definition, rejected submissions can be "un-rejected" if investigation context changes (e.g., previously unrelated data becomes relevant after a new hypothesis emerges). This is the **only** supported category transition:
- `REJECTED` → one of the 5 valid categories (SYMPTOM, CAUSAL, MITIGATION, SOLUTION, CONTEXTUAL)
- All other category transitions are not supported

**When un-rejecting occurs**, the system must:
1. Update `category` to the new classification
2. Recalculate `advances_milestones` using the category-milestone mapping
3. Emit an audit event recording the reclassification and reasoning

**For non-REJECTED misclassifications**, the intended remediation path is:
1. The user or agent acknowledges the misclassification in the conversation
2. A new submission with corrected context can be provided if needed
3. The LLM accounts for the misclassified evidence when reasoning about hypotheses (e.g., "Evidence ev_abc was classified as SYMPTOM but appears to be baseline context")

---

### Decision 5: Unified DataType (Shared with Preprocessing)

Evidence uses the same `DataType` enum as preprocessing. The canonical enum definition (including per-type description, detection rules, and confidence scoring) lives in [Data Classification Strategy → Unified DataType Enum](./data-classification-strategy.md#unified-datatype-enum-6-types). The version reproduced below is a reader-convenience copy with evidence-specific usage notes; the canonical doc governs additions, removals, and semantic changes.

```python
class DataType(str, Enum):
    """Data type classification — shared across preprocessing and evidence"""

    LOGS = "logs"
    """
    Time-ordered diagnostic output.

    Includes: Application logs, system logs, command output, distributed traces,
    API responses, error messages, stack traces.
    """

    METRICS = "metrics"
    """
    Quantitative measurements.

    Includes: Time-series metrics, dashboards, performance data,
    resource usage, monitoring alerts, profiling output.
    """

    CONFIGURATION = "configuration"
    """
    Structured system/application configuration.

    Includes: Config files (YAML, JSON, TOML, env vars), infrastructure
    definitions (K8s manifests, Terraform), database schema.
    """

    CODE = "code"
    """
    Source code files.

    Includes: Application code, scripts, infrastructure-as-code,
    database migrations, build configurations.
    """

    TEXT = "text"
    """
    Unstructured prose.

    Includes: Documentation, runbooks, incident reports, problem
    descriptions, steps to reproduce, user observations.

    Note: User-typed text input (form=USER_TEXT) uses this type.
    """

    IMAGE = "image"
    """
    Visual content.

    Includes: Screenshots, architecture diagrams, dashboard captures,
    terminal screenshots, graphs and charts.
    """
```

**Rationale:**
1. **Single taxonomy** — no mapping between preprocessing DataType and evidence source type
2. **Easier for LLM** — 6 clear, non-overlapping choices
3. **Fine-grained detail preserved** — preprocessing metadata captures subtypes (e.g., `DataType.LOGS` with `metadata.subtype = "distributed_trace"`)
4. **`form` field handles input channel** — `DOCUMENT` (attachments: file uploads and pasted data) vs `USER_TEXT` (query-only turns) vs `SUBMITTED_DATA` (agent tool findings)

**CSV/TSV Best-Effort Fallback**: CSV and TSV files that fail the primary metrics check (metrics_score < 2) are classified as `UNSTRUCTURED_TEXT` directly in the best-effort fallback section of `DataClassifier`. This avoids false positives from vocabulary-based scoring, where incidental cell content (e.g., "error", "cpu", "interface") matches log, metric, or code patterns. Reference tables and non-numeric CSVs are correctly classified as text rather than competing across all DataType scores.

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

Ingestion Pipeline (before LLM):
- form: DOCUMENT  ← Set by payload context (attachment present)
- data_type: LOGS  ← Classified by Tier 0+1 (before LLM)

LLM Classification:
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
# Illustrative subset — see faultmaven/modules/case/domain/models.py for the canonical Evidence model.
class Evidence(BaseModel):
    """
    Investigation evidence and analyzed submissions.

    IMPORTANT SEMANTIC NOTE:
    This model tracks both:
    1. Valid evidence (SYMPTOM, CAUSAL, MITIGATION, SOLUTION, CONTEXTUAL)
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

    Note: Evidence is owned via Case.evidence and does NOT carry its own case_id field.
    The relationship to hypotheses is one-way through Hypothesis.evidence_links.
    """

    # Identity
    evidence_id: str = Field(pattern=r"^ev_[a-f0-9]{12}$")

    # Classification (assigned by LLM at creation time)
    category: EvidenceCategory  # SYMPTOM/CAUSAL/MITIGATION/SOLUTION/CONTEXTUAL/REJECTED
    data_type: Optional[str] = None  # Free-form string column (LOGS/METRICS/CONFIGURATION/CODE/TEXT/IMAGE)
    form: EvidenceForm  # DOCUMENT (attachments) or USER_TEXT (query-only) or SUBMITTED_DATA (agent tools)
    source_type: Optional[str] = None  # e.g. "user_upload", "agent_extracted"

    # Content
    summary: str = Field(max_length=500)
    primary_purpose: str  # What this evidence shows, or why rejected if REJECTED
    content_ref: str  # storage reference (local path or S3 URI, depending on STORAGE_BACKEND)
    preprocessed_content: Optional[str] = None  # Cleaned/normalized content
    preprocessing_method: Optional[str] = None  # Pipeline stage that produced preprocessed_content
    compression_ratio: Optional[float] = None
    analysis: Optional[str] = None  # Free-form analytical notes
    original_filename: Optional[str] = None  # Display filename (e.g., "app.log")
    source_file_id: Optional[str] = None     # FK to uploaded_files when applicable

    # Metadata
    collected_at: datetime
    collected_by: str  # user_id
    collected_at_turn: int

    # Processing (from PreprocessingResult)
    extraction_method: str  # Tier 1 method: "structural_index", "statistical_profile",
                            # "parse_and_sanitize", "ast_extraction", "structure_extraction",
                            # "metadata_extraction", "page_capture_passthrough", or "none"
    content_size_bytes: int
    content_hash: Optional[str] = None  # SHA-256 for deduplication

    # Processing mode tracking (v5.0+)
    processing_mode: Optional[str] = None  # triage | directed_analysis | knowledge_query | semantic_search
    da_invocation_count: int = 0  # Cross-turn DA call counter; read by the secondary
                                  # /sessions/execute path (agent_orchestration_service) to
                                  # reconstruct EvidenceDAState across turns. The primary
                                  # /turns path tracks failures via in-memory counters.

    # Investigation linkage
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
    category VARCHAR(50) NOT NULL,  -- symptom_evidence, causal_evidence, resolution_evidence, contextual_evidence, rejected
    data_type VARCHAR(50) NOT NULL,  -- logs, metrics, configuration, code, text, image (unified DataType)
    form VARCHAR(20) NOT NULL,

    -- Content
    summary TEXT NOT NULL,
    primary_purpose TEXT NOT NULL,
    content_ref VARCHAR(500) NOT NULL,
    -- NOTE: structural_index is stored in Vector DB (ChromaDB), not in this table.
    -- It is too large (50KB+ for logs) to store inline. Use vector search for retrieval.

    -- Metadata
    collected_at TIMESTAMP WITH TIME ZONE NOT NULL,
    collected_by VARCHAR(36) NOT NULL,
    collected_at_turn INTEGER NOT NULL,

    -- Processing (from PreprocessingResult)
    extraction_method VARCHAR(50) NOT NULL,  -- structural_index, statistical_profile, etc.
    content_size_bytes INTEGER NOT NULL,
    content_hash VARCHAR(64),  -- SHA-256

    -- Processing mode tracking (v5.0+)
    processing_mode VARCHAR(20),           -- triage | directed_analysis | knowledge_query | semantic_search
    da_invocation_count INTEGER DEFAULT 0  -- Cross-turn DA call counter; read by agent_orchestration_service
                                           -- to reconstruct EvidenceDAState on the secondary /sessions/execute path
);

-- NOTE (Gap #20): The previous `UNIQUE (case_id, collected_at_turn)` constraint was removed —
-- multiple evidence items per turn are supported. Deduplication is done at the application
-- layer via content_hash lookup before insert, not via a SQL UNIQUE constraint.

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

### User Submits Turn with Attachment

```
1. User submits turn via POST /api/v1/cases/{case_id}/turns
   {query?: "Can you check this?", files: [app.log], pasted_content?: null}
   ↓
2. STEP 1 — PRE-LLM PREPROCESSING (for each attachment):
   - _preprocess_attachment(case, attachment):
     - Tier 0: Classify data type (DataClassifier)
     - Tier 1: Type-specific extraction (structural index)
     - Compute content_hash (SHA-256)
     - Store raw file via storage_service
     - Create Evidence(form=DOCUMENT, preprocessed_content=structural_index)
   ↓
3. Determine query:
   - If user provided query → use it
   - If no query + attachments → generate_implicit_query()
   ↓
4. Save user message to case.messages[]
   ↓
5. STEP 2 — LLM INFERENCE:
   - Context includes structural indexes via Context Sliding Window
   - LLM receives: full case context + query + evidence structural indexes
   ↓
6. LLM returns structured response with:
   - state_updates.evidence_to_add: [{
       category: "symptom_evidence",
       data_type: "logs",
       summary: "Database connection timeout errors",
       primary_purpose: "Shows connection pool exhaustion at peak hours"
     }]
   (Agent findings → form=SUBMITTED_DATA, set by milestone engine)
   ↓
7. Build TurnResponse with attachments_processed[]
   ↓
8. Save to database and return response
```

### User Submits Query-Only Turn

```
1. User submits turn via POST /api/v1/cases/{case_id}/turns
   {query: "Why is CPU high?", files: [], pasted_content: null}
   ↓
2. STEP 1 SKIPPED (no attachments)
   ↓
3. Save user message to case.messages[]
   ↓
4. STEP 2 — LLM INFERENCE:
   - LLM receives: full case context + query
   ↓
5. LLM returns:
   - agent_response: "..."
   - NO evidence_to_add (typically)
   ↓
6. NO attachment evidence created (query-only turn)
   ↓
7. Return TurnResponse {attachments_processed: []}
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
    EvidenceCategory.MITIGATION_EVIDENCE: [
        "mitigation_applied",
    ],
    EvidenceCategory.SOLUTION_EVIDENCE: [
        "solution_applied",
        "solution_verified",
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
# Illustrative subset — see faultmaven/core/investigation/schemas.py for the canonical EvidenceToAdd.
class EvidenceToAdd(BaseModel):
    summary: str
    content_ref: str            # Storage reference for the evidence content
    category: EvidenceCategory  # SYMPTOM/CAUSAL/MITIGATION/SOLUTION/CONTEXTUAL/REJECTED
    source_type: str            # e.g. "user_upload", "agent_extracted"
    likelihood: float           # LLM-assessed confidence (0.0-1.0)

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

> **Note:** `EvidenceToAdd` does **not** carry `data_type` — that's classified downstream during preprocessing/storage. The structured-output schema the LLM produces is intentionally narrower than the persisted `Evidence` row.

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

---

## Related Documentation

- [Data Preprocessing Design Specification](./data-preprocessing-design-specification.md) — Four-tier preprocessing model, unified ingestion pipeline, and shared DataType enum
- [Data Classification Strategy](./data-classification-strategy.md) — Tier 0 classification rules for DataType detection
- [Evidence Flow Architecture](./evidence-flow-architecture.md) — End-to-end evidence pipeline diagrams
- [Evidence Failure Modes](./evidence-failure-modes.md) — Failure handling design for single-phase creation

---

**Document Version:** 2.2
**Last Updated:** 2026-02-23
**Status:** Design Specification
