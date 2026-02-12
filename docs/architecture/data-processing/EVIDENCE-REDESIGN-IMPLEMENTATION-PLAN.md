# Evidence Classification Redesign - Implementation Plan

**Date:** 2026-02-11 (Updated with failure mode handling)
**Implementation Date:** 2026-02-11
**Status:** ✅ **COMPLETED** (Phases 1-6, 8 complete; Phase 7 deferred to post-MVP)

**Design References:**
- [EVIDENCE-CLASSIFICATION-FINAL-DESIGN.md](./EVIDENCE-CLASSIFICATION-FINAL-DESIGN.md) - ✅ IMPLEMENTED
- [EVIDENCE-CREATION-FAILURE-MODES.md](./EVIDENCE-CREATION-FAILURE-MODES.md) - ⏳ DEFERRED
- [MILESTONE-ADVANCEMENT-ANALYSIS.md](./archive/2026-02/MILESTONE-ADVANCEMENT-ANALYSIS.md) - Design analysis (archived)

**Implementation Summary:**
- [EVIDENCE-REDESIGN-CHANGELOG.md](./EVIDENCE-REDESIGN-CHANGELOG.md) - Complete implementation changelog
- [EVIDENCE-REDESIGN-IMPLEMENTATION-SUMMARY.md](./archive/2026-02/EVIDENCE-REDESIGN-IMPLEMENTATION-SUMMARY.md) - Detailed implementation summary (archived)

---

## Overview

This document provides a detailed, phased implementation plan for the evidence classification redesign. The work is broken down into discrete tasks that can be assigned to developers and tracked independently.

**Key Design Elements:**
1. Single-phase evidence creation (after LLM evaluation)
2. REJECTED category for tracking rejected submissions
3. CONTEXTUAL_EVIDENCE category for baseline/environmental data
4. Simplified source types (5 instead of 12)
5. **Failure mode handling** with async retries and storage cleanup
6. **Milestone advancement attribution** (Option 2.5: system-inferred with LLM override)

**Estimated Effort:** 7-9 days (1 developer, includes failure handling and retry infrastructure)

---

## Implementation Phases

### Phase 1: Database Schema Updates
**Duration:** 1 day
**Dependencies:** None
**Risk:** Low (additive changes only)

#### Task 1.1: Create Alembic Migration
**File:** `faultmaven/infrastructure/persistence/alembic/versions/XXX_evidence_classification_redesign.py`

**Changes:**
```python
# 1. Add new enum values to EvidenceCategory
op.execute("""
    ALTER TYPE evidencecategory ADD VALUE 'contextual_evidence';
    ALTER TYPE evidencecategory ADD VALUE 'rejected';
""")

# 2. Create new EvidenceSourceType enum with 5 values
op.execute("""
    CREATE TYPE evidencesourcetype_new AS ENUM (
        'logs',
        'metrics',
        'configuration',
        'visual',
        'user_description'
    );
""")

# 3. Migrate existing source_type values to new enum
op.execute("""
    ALTER TABLE evidence
    ADD COLUMN source_type_new evidencesourcetype_new;

    UPDATE evidence SET source_type_new = CASE
        WHEN source_type IN ('log_file', 'command_output', 'trace_data', 'api_response') THEN 'logs'
        WHEN source_type IN ('metrics_data', 'monitoring_alert') THEN 'metrics'
        WHEN source_type IN ('config_file', 'code_review', 'database_query') THEN 'configuration'
        WHEN source_type = 'screenshot' THEN 'visual'
        WHEN source_type = 'user_report' THEN 'user_description'
        ELSE 'logs'  -- Default for 'other'
    END;

    ALTER TABLE evidence DROP COLUMN source_type;
    ALTER TABLE evidence RENAME COLUMN source_type_new TO source_type;
""")

# 4. Add database constraints
op.create_unique_constraint(
    'uq_evidence_case_turn',
    'evidence',
    ['case_id', 'collected_at_turn']
)

op.create_unique_constraint(
    'uq_evidence_case_hash',
    'evidence',
    ['case_id', 'content_hash']
)

# 5. Add indexes
op.create_index(
    'idx_evidence_case_category',
    'evidence',
    ['case_id', 'category']
)

op.create_index(
    'idx_evidence_case_turn',
    'evidence',
    ['case_id', 'collected_at_turn']
)

op.create_index(
    'idx_evidence_hash',
    'evidence',
    ['case_id', 'content_hash'],
    postgresql_where='content_hash IS NOT NULL'
)
```

**Testing:**
- [ ] Migration runs successfully on empty database
- [ ] Migration runs successfully on database with existing evidence
- [ ] All constraints are created
- [ ] All indexes are created
- [ ] Rollback works correctly

---

#### Task 1.2: Migrate Existing Data
**File:** Same migration file

**Data Migration:**
```python
# Migrate OTHER → CONTEXTUAL_EVIDENCE
op.execute("""
    UPDATE evidence
    SET category = 'contextual_evidence'
    WHERE category = 'other';
""")

# Handle UNCLASSIFIED evidence
# Option A: Mark as REJECTED with explanation
op.execute("""
    UPDATE evidence
    SET category = 'rejected',
        primary_purpose = CONCAT(
            '[Migrated from UNCLASSIFIED] ',
            COALESCE(primary_purpose, 'Legacy unclassified evidence')
        )
    WHERE category = 'unclassified';
""")

# Option B: Try to infer category based on content (more complex)
# See implementation notes below
```

**Implementation Notes:**
- Review existing UNCLASSIFIED evidence to determine best migration strategy
- Consider manual review of UNCLASSIFIED evidence before migration
- Log all migrations for audit trail

**Testing:**
- [ ] All OTHER → CONTEXTUAL_EVIDENCE
- [ ] All UNCLASSIFIED handled appropriately
- [ ] No data loss
- [ ] primary_purpose field updated correctly

---

#### Task 1.3: Update SQLAlchemy Models
**File:** `faultmaven/infrastructure/persistence/models.py`

**Changes:**
```python
# Update enum mapping (lines ~283-325)
from faultmaven.modules.case.domain.models import (
    EvidenceCategory,  # Updated enum
    EvidenceSourceType,  # Updated enum
    # ...
)

# Verify column definitions match new constraints
class EvidenceModel(Base):
    __tablename__ = "evidence"

    # ... existing fields ...

    category = Column(
        Enum(EvidenceCategory),
        nullable=False,
    )

    source_type = Column(
        Enum(EvidenceSourceType),
        nullable=False,
    )

    # Ensure constraints are reflected
    __table_args__ = (
        UniqueConstraint('case_id', 'collected_at_turn', name='uq_evidence_case_turn'),
        UniqueConstraint('case_id', 'content_hash', name='uq_evidence_case_hash'),
        Index('idx_evidence_case_category', 'case_id', 'category'),
        Index('idx_evidence_case_turn', 'case_id', 'collected_at_turn'),
        Index('idx_evidence_hash', 'case_id', 'content_hash', postgresql_where=text('content_hash IS NOT NULL')),
    )
```

**Testing:**
- [ ] Models load without errors
- [ ] Enum values match domain models
- [ ] Constraints are defined
- [ ] Can create/read/update/delete evidence records

---

### Phase 2: Domain Model Updates
**Duration:** 1 day
**Dependencies:** Phase 1 complete
**Risk:** Medium (affects core domain logic)

#### Task 2.1: Update Evidence Enums
**File:** `faultmaven/modules/case/domain/models.py`

**Changes:**
```python
# Lines 1191-1281: Update EvidenceCategory
class EvidenceCategory(str, Enum):
    """Evidence classification by investigation purpose"""

    # Remove UNCLASSIFIED
    # UNCLASSIFIED = "unclassified"  # DEPRECATED - removed in redesign

    SYMPTOM_EVIDENCE = "symptom_evidence"
    """Shows problem manifestation"""

    CAUSAL_EVIDENCE = "causal_evidence"
    """Points to root cause"""

    RESOLUTION_EVIDENCE = "resolution_evidence"
    """Validates fix effectiveness"""

    CONTEXTUAL_EVIDENCE = "contextual_evidence"  # Was: OTHER
    """
    Provides baseline, environmental, or background context.

    Examples:
    - System architecture diagrams
    - Baseline configuration files
    - Normal resource usage patterns
    - System inventory (versions, dependencies)
    """

    REJECTED = "rejected"
    """
    Submission analyzed but rejected as not useful for investigation.

    IMPORTANT: This is NOT evidence. Tracked for deduplication and audit.
    """

# Lines 1284-1298: Update EvidenceSourceType
class EvidenceSourceType(str, Enum):
    """Fundamental type of data source"""

    LOGS = "logs"
    """Any textual diagnostic output"""

    METRICS = "metrics"
    """Quantitative measurements"""

    CONFIGURATION = "configuration"
    """System/application configuration"""

    VISUAL = "visual"
    """Visual representations (screenshots, diagrams)"""

    USER_DESCRIPTION = "user_description"
    """User's typed narrative"""
```

**Testing:**
- [ ] All enum values are valid
- [ ] Documentation is complete
- [ ] No references to deprecated values in tests
- [ ] Serialization/deserialization works

---

#### Task 2.2: Add Submission Classification Models
**File:** `faultmaven/core/investigation/schemas.py`

**Changes:**
```python
# Add new classification model (insert after InternalReasoning)
class SubmissionClassification(BaseModel):
    """Classification of user's submission content"""

    type: Literal["user_chat", "external_data", "mixed"] = Field(
        description=(
            "user_chat: Pure conversation → NO evidence record\n"
            "external_data: Data from elsewhere → Evidence record\n"
            "mixed: Both chat and data → Evidence record (extract data)"
        )
    )

    confidence: Literal["high", "medium", "low"] = Field(
        description="Confidence in classification"
    )

    reasoning: str = Field(
        description="Brief explanation of classification decision",
        max_length=200,
    )

    external_data_summary: Optional[str] = Field(
        None,
        description="If external_data or mixed, summarize what data is present",
        max_length=200,
    )

# Update all response schemas to include submission_classification
class BaseInteractionResponse(BaseModel):
    agent_response: str
    internal_reasoning: InternalReasoning
    state_updates: StateUpdates
    submission_classification: SubmissionClassification  # NEW

# Apply to all schema models:
# - InquiryResponse
# - SymptomVerificationResponse
# - CausalInvestigationResponse
# - SolutionProposalResponse
# - ResolutionVerificationResponse
# - TerminalResponse
```

**Testing:**
- [ ] Schema models validate correctly
- [ ] JSON serialization/deserialization works
- [ ] LLM can generate valid responses (integration test)

---

### Phase 3: Prompt Updates
**Duration:** 0.5 day
**Dependencies:** Phase 2 complete
**Risk:** Low (prompt changes are iterative)

#### Task 3.1: Add Classification Guidance to Prompts
**File:** `faultmaven/core/investigation/prompts/templates.py`

**Changes:**
```python
# Add new prompt section
SUBMISSION_CLASSIFICATION_GUIDANCE = """
# Submission Classification

For each user submission, classify the content type:

**user_chat:** Pure conversational text (no evidence record created)
- Questions: "Why is the CPU usage high?"
- Acknowledgments: "OK, I'll check that"
- Discussion: "I think it might be the database"
- Planning: "Let me investigate the logs"

**external_data:** Data from external sources (create evidence record)
- Log excerpts: "2024-01-10 ERROR: Connection timeout..."
- Error messages: "OperationalError: no such column"
- Metrics: "CPU: 95%, Memory: 12GB"
- Command output: "$ df -h\\nFilesystem  Size  Used..."
- Screenshots of dashboards, terminals, errors

**mixed:** Both chat and external data (create evidence record)
- "Here are the logs: [log content]"
- "I'm seeing this error: [error text]"
- Chat text + file attachments

## Evidence Category Classification

When classifying evidence, ask:

1. Does it show the PROBLEM happening? → SYMPTOM_EVIDENCE
2. Does it point to the ROOT CAUSE? → CAUSAL_EVIDENCE
3. Does it prove the FIX worked? → RESOLUTION_EVIDENCE
4. Does it provide CONTEXT/BASELINE (not problem/cause/fix)? → CONTEXTUAL_EVIDENCE
5. Is it unrelated to this case? → REJECTED

## Source Type Classification

Choose from 5 fundamental types:

- **logs**: Textual diagnostic output (logs, command output, traces, API responses)
- **metrics**: Quantitative measurements (time-series, dashboards, alerts)
- **configuration**: System config (config files, code, schema, infrastructure)
- **visual**: Visual representations (screenshots, diagrams, graphs)
- **user_description**: User's typed narrative (problem description, observations)

Classify with high confidence when clear, medium/low when ambiguous.
"""

# Update phase-specific prompts to include this guidance
def get_prompt_for_case(...):
    # ... existing code ...

    prompt += SUBMISSION_CLASSIFICATION_GUIDANCE

    # ... rest of prompt
```

**Testing:**
- [ ] Prompts compile without errors
- [ ] Token count acceptable (< context limit)
- [ ] LLM generates valid classifications (manual testing)

---

### Phase 4: Core Logic Updates
**Duration:** 2 days
**Dependencies:** Phases 1-3 complete
**Risk:** High (changes investigation flow)

#### Task 4.1: Remove UNCLASSIFIED Evidence Auto-Creation
**File:** `faultmaven/core/investigation/milestone_engine.py`

**Changes:**
```python
# Lines 359-400: REMOVE this entire block
# Comment out or delete:
"""
# Store user message as unclassified data item with an ID
user_evidence = Evidence(
    evidence_id=f"ev_{uuid4().hex[:12]}",
    summary=user_message[:200] + ("..." if len(user_message) > 200 else ""),
    content_ref=f"turn_{case.current_turn + 1}_user_message",
    category=EvidenceCategory.UNCLASSIFIED,  # ← DEPRECATED
    source_type=EvidenceSourceType.USER_REPORT,
    # ...
)
case.evidence.append(user_evidence)
"""

# Replace with comment:
"""
NOTE: Evidence is now created AFTER LLM evaluation, not before.
The LLM classifies the submission and only creates evidence if relevant.
See _process_response_structured() for evidence creation logic.
"""
```

**Testing:**
- [ ] process_turn() still works without auto-creation
- [ ] No UNCLASSIFIED evidence created for new cases
- [ ] LLM can still reference evidence (via new_index_N pattern)

---

#### Task 4.2: Implement Classification-Based Evidence Creation
**File:** `faultmaven/core/investigation/milestone_engine.py`

**Changes:**
```python
# In _process_response_structured() (lines ~769-771)
async def _process_response_structured(
    self,
    case: Case,
    user_message: str,
    response_obj: BaseInteractionResponse,
    attachments: Optional[List[dict]] = None,
) -> Tuple[Case, dict]:
    """Process structured LLM response and update case state"""

    # NEW: Extract submission classification
    classification = response_obj.submission_classification

    logger.info(
        f"Submission classified as: {classification.type} "
        f"(confidence: {classification.confidence})"
    )

    # Handle based on classification type
    if classification.type == "user_chat":
        # Pure chat - NO evidence created
        logger.info("Pure chat submission - no evidence record created")
        # Continue with normal processing (agent response, etc.)

    elif classification.type in ["external_data", "mixed"]:
        # Data submission - create evidence record
        await self._create_evidence_from_submission(
            case=case,
            classification=classification,
            user_message=user_message,
            attachments=attachments,
            state_updates=response_obj.state_updates,
        )

    # ... rest of existing logic ...
```

**New Method:**
```python
async def _create_evidence_from_submission(
    self,
    case: Case,
    classification: SubmissionClassification,
    user_message: str,
    attachments: Optional[List[dict]],
    state_updates: StateUpdates,
) -> None:
    """
    Create evidence record from user submission.

    Evidence is created based on:
    1. LLM's classification (user_chat/external_data/mixed)
    2. LLM's state_updates.evidence_to_add (category, source_type, etc.)
    3. Attachment metadata (if file upload)
    """

    # Check if LLM added evidence in state_updates
    if not state_updates.evidence_to_add:
        logger.warning(
            f"Classification={classification.type} but no evidence_to_add. "
            "Creating REJECTED evidence for tracking."
        )
        # Create REJECTED evidence for audit trail
        evidence = Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            summary=classification.external_data_summary or user_message[:200],
            category=EvidenceCategory.REJECTED,
            source_type=self._infer_source_type(user_message, attachments),
            primary_purpose=classification.reasoning,
            # ... metadata fields ...
        )
        case.evidence.append(evidence)
        return

    # Create evidence from LLM's specification
    for ev_item in state_updates.evidence_to_add:
        # Check for duplicates via content_hash
        content_hash = self._calculate_content_hash(attachments or user_message)
        existing = self._find_duplicate_evidence(case, content_hash)

        if existing:
            logger.info(
                f"Duplicate evidence detected (matches {existing.evidence_id} "
                f"from turn {existing.collected_at_turn})"
            )
            # Create evidence record noting it's a duplicate
            evidence = Evidence(
                evidence_id=f"ev_{uuid4().hex[:12]}",
                category=EvidenceCategory.REJECTED,
                summary=f"Duplicate of evidence from turn {existing.collected_at_turn}",
                primary_purpose=f"Same content as {existing.evidence_id}: {existing.summary}",
                content_hash=content_hash,
                # ... metadata ...
            )
        else:
            # Create new evidence
            evidence = Evidence(
                evidence_id=f"ev_{uuid4().hex[:12]}",
                category=ev_item.category,
                source_type=ev_item.source_type,
                summary=ev_item.summary,
                primary_purpose=ev_item.primary_purpose,
                content_hash=content_hash,
                # ... from attachments or user_message ...
            )

        case.evidence.append(evidence)
        logger.info(f"Created evidence {evidence.evidence_id} ({evidence.category})")
```

**Helper Methods:**
```python
def _calculate_content_hash(self, content: Union[str, List[dict]]) -> Optional[str]:
    """Calculate SHA256 hash for deduplication"""
    if isinstance(content, list) and content:
        # File upload - use file content hash
        return content[0].get("content_hash")
    elif isinstance(content, str):
        # Text submission - hash the text
        return hashlib.sha256(content.encode()).hexdigest()
    return None

def _find_duplicate_evidence(
    self,
    case: Case,
    content_hash: Optional[str]
) -> Optional[Evidence]:
    """Check if evidence with same hash already exists for this case"""
    if not content_hash:
        return None

    for ev in case.evidence:
        if ev.content_hash == content_hash:
            return ev
    return None

def _infer_source_type(
    self,
    user_message: str,
    attachments: Optional[List[dict]]
) -> EvidenceSourceType:
    """Infer source type if LLM didn't specify"""
    if attachments:
        data_type = attachments[0].get("data_type", "")
        if "log" in data_type:
            return EvidenceSourceType.LOGS
        elif "metric" in data_type or "dashboard" in data_type:
            return EvidenceSourceType.METRICS
        elif "config" in data_type or "code" in data_type:
            return EvidenceSourceType.CONFIGURATION
        elif "image" in data_type or "screenshot" in data_type:
            return EvidenceSourceType.VISUAL
        else:
            return EvidenceSourceType.LOGS  # Default for files
    else:
        return EvidenceSourceType.USER_DESCRIPTION  # Text input
```

**Testing:**
- [ ] user_chat → no evidence created
- [ ] external_data → evidence created with correct category
- [ ] mixed → evidence created
- [ ] Duplicate detection works (same file uploaded twice)
- [ ] REJECTED evidence created for rejected submissions
- [ ] content_hash populated correctly

---

#### Task 4.3: Update Evidence Reference Validation
**File:** `faultmaven/core/investigation/milestone_engine.py`

**Changes:**
```python
# Lines 205-240: Update _validate_evidence_references()
# Ensure it still supports "new_index_N" pattern for evidence being created this turn

# Example: User uploads file, LLM says:
# "Based on new_index_0, the connection pool is exhausted"
# This should validate even though evidence doesn't exist yet

# NO CHANGES NEEDED - this already works correctly
# Just verify with tests
```

**Testing:**
- [ ] new_index_N references validate correctly
- [ ] Evidence created this turn can be referenced by hypotheses
- [ ] Invalid references still fail validation

---

### Phase 5: API Updates
**Duration:** 0.5 day
**Dependencies:** Phase 4 complete
**Risk:** Low (minimal API changes)

#### Task 5.1: Update Response Models
**File:** `faultmaven/modules/case/api/models.py`

**Changes:**
```python
# Add submission classification to response (if exposed to API)
class CaseQueryResponse(BaseModel):
    case_id: str
    turn_number: int
    agent_response: str
    # ... existing fields ...

    # Optional: Expose classification to frontend for analytics
    submission_classification: Optional[SubmissionClassification] = None
```

**Testing:**
- [ ] API responses serialize correctly
- [ ] Frontend can consume new fields (if exposed)

---

#### Task 5.2: Update File Upload Endpoint
**File:** `faultmaven/modules/case/api/routes.py`

**Changes:**
```python
# Lines 2405-2501: Update /data/ endpoint preprocessing
# Ensure content_hash is calculated and passed to process_turn()

@router.post("/cases/{case_id}/data")
async def upload_case_data(...):
    # ... existing preprocessing ...

    # Calculate content hash for deduplication
    content_hash = hashlib.sha256(file_content).hexdigest()

    attachment_metadata = {
        "file_id": file_id,
        "filename": file.filename,
        "data_type": detected_type,
        "summary": preprocessing_summary,
        "content_hash": content_hash,  # NEW
        # ... existing fields ...
    }

    # Rest of flow unchanged
```

**Testing:**
- [ ] File uploads include content_hash
- [ ] Duplicate uploads detected
- [ ] File metadata passed correctly to milestone_engine

---

### Phase 6: Testing
**Duration:** 1 day
**Dependencies:** Phases 1-5 complete
**Risk:** Medium (comprehensive testing needed)

#### Task 6.1: Unit Tests

**File:** `tests/unit/core/investigation/test_evidence_classification.py` (NEW)

**Tests:**
```python
class TestEvidenceClassification:
    """Test evidence classification logic"""

    def test_user_chat_no_evidence_created(self):
        """Pure chat should not create evidence record"""
        # Submit "Why is CPU high?"
        # Assert: No evidence added to case

    def test_external_data_creates_evidence(self):
        """External data should create evidence with category"""
        # Submit log file
        # Assert: Evidence created with category=SYMPTOM_EVIDENCE

    def test_rejected_data_tracked(self):
        """Rejected submissions should create REJECTED evidence"""
        # Submit vacation photo
        # Assert: Evidence created with category=REJECTED

    def test_duplicate_detection(self):
        """Uploading same file twice should be detected"""
        # Upload log file (turn 1)
        # Upload same log file (turn 2)
        # Assert: Second upload marked as duplicate

    def test_contextual_evidence_classification(self):
        """Baseline/context data should use CONTEXTUAL_EVIDENCE"""
        # Submit architecture diagram
        # Assert: category=CONTEXTUAL_EVIDENCE

    def test_source_type_classification(self):
        """Source types should be correctly classified"""
        # Submit various file types
        # Assert: Correct source_type (logs/metrics/config/visual)
```

**File:** `tests/unit/core/investigation/test_milestone_engine.py` (UPDATE)

**Updates:**
```python
# Update existing tests to remove UNCLASSIFIED expectations
# Add tests for new classification flow

class TestMilestoneEngine:
    def test_no_unclassified_evidence_created(self):
        """process_turn should not auto-create UNCLASSIFIED evidence"""
        # Call process_turn with user message
        # Assert: No evidence with category=UNCLASSIFIED

    def test_evidence_created_after_llm(self):
        """Evidence created based on LLM classification"""
        # Mock LLM response with evidence_to_add
        # Call process_turn
        # Assert: Evidence created with LLM-specified category
```

---

#### Task 6.2: Integration Tests

**File:** `tests/integration/test_evidence_flow.py` (NEW)

**Tests:**
```python
class TestEvidenceFlowIntegration:
    """End-to-end evidence creation flow"""

    async def test_file_upload_to_evidence(self):
        """Complete flow: upload → preprocess → LLM → evidence"""
        # Upload log file via /data/ endpoint
        # Verify preprocessing generates content_hash
        # Verify LLM classifies as external_data
        # Verify evidence created in database
        # Verify evidence retrievable via case query

    async def test_duplicate_file_rejection(self):
        """Duplicate file uploads should be tracked"""
        # Upload file1.log
        # Upload file1.log again
        # Verify: Second evidence record is REJECTED
        # Verify: References first evidence in primary_purpose

    async def test_chat_analytics_query(self):
        """Analytics: submissions vs accepted evidence"""
        # Create case with mix of chat, relevant files, irrelevant files
        # Query: SELECT COUNT(*) FROM evidence WHERE category != 'rejected'
        # Verify: Count matches expected relevant evidence
```

---

#### Task 6.3: Manual Testing Checklist

**Test Scenarios:**
- [ ] Upload relevant log file → Evidence created (SYMPTOM_EVIDENCE)
- [ ] Upload config file → Evidence created (CONTEXTUAL_EVIDENCE)
- [ ] Upload vacation photo → Evidence created (REJECTED)
- [ ] Upload same file twice → Second marked as duplicate
- [ ] Send pure chat message → No evidence created
- [ ] Send chat + file → Evidence created
- [ ] Check analytics queries work (acceptance rate, rejection reasons)
- [ ] Verify LLM prompts include classification guidance
- [ ] Verify database constraints prevent duplicate turn_number
- [ ] Verify database constraints prevent duplicate content_hash per case

---

### Phase 7: Failure Mode Handling & Retry Infrastructure
**Duration:** 1.5 days
**Dependencies:** Phase 4 complete (evidence creation logic)
**Risk:** Medium (async job infrastructure)
**Reference:** [EVIDENCE-CREATION-FAILURE-MODES.md](./EVIDENCE-CREATION-FAILURE-MODES.md)

#### Task 7.1: Add Category Fallback Validation

**File:** `faultmaven/core/investigation/schemas.py`

**Changes:**
```python
class EvidenceToAdd(BaseModel):
    category: EvidenceCategory

    @validator('category', pre=True)
    def validate_category(cls, v):
        """Fallback to CONTEXTUAL_EVIDENCE for unrecognized categories"""
        if isinstance(v, str):
            try:
                return EvidenceCategory(v)
            except ValueError:
                logger.warning(
                    f"LLM returned unrecognized category '{v}', "
                    f"falling back to CONTEXTUAL_EVIDENCE",
                    extra={
                        "category_attempted": v,
                        "alert_team": "llm_integration"
                    }
                )
                return EvidenceCategory.CONTEXTUAL_EVIDENCE
        return v
```

**Testing:**
- [ ] Test with invalid category string
- [ ] Verify CONTEXTUAL_EVIDENCE fallback
- [ ] Verify warning logged with alerting context

---

#### Task 7.2: Implement Async Retry for LLM Analysis

**File:** `faultmaven/modules/agent/jobs/evidence_retry.py` (NEW)

**Implementation:**
```python
async def retry_evidence_analysis(
    case_id: str,
    content_ref: str,
    content_hash: str,
    user_message: str,
    retry_count: int,
    max_retries: int = 3
):
    """Background job to retry failed LLM analysis"""

    if retry_count >= max_retries:
        logger.error(f"Max retries reached for {content_ref}")
        # Create REJECTED evidence as fallback
        await create_rejected_evidence(
            case_id=case_id,
            content_ref=content_ref,
            content_hash=content_hash,
            reason="Analysis failed after multiple retries"
        )
        return

    try:
        case = await case_service.get(case_id)
        result = await llm_service.analyze(
            case, user_message, content_ref,
            timeout=60  # Longer timeout for retries
        )
        await create_evidence(case, result)
        logger.info(f"Retry {retry_count + 1} successful for {content_ref}")

    except LLMError as e:
        logger.warning(f"Retry {retry_count + 1} failed: {e}")
        # Exponential backoff
        delay = 2 ** retry_count * 60  # 1min, 2min, 4min
        await job_queue.enqueue_delayed(
            "retry_evidence_analysis",
            delay_seconds=delay,
            case_id=case_id,
            content_ref=content_ref,
            content_hash=content_hash,
            user_message=user_message,
            retry_count=retry_count + 1,
            max_retries=max_retries
        )
```

**Testing:**
- [ ] Test successful retry after timeout
- [ ] Test exponential backoff delays
- [ ] Test max retries creates REJECTED evidence
- [ ] Test retry doesn't duplicate evidence (content_hash check)

---

#### Task 7.3: Implement Async Retry for DB Insert

**File:** `faultmaven/modules/agent/jobs/evidence_retry.py` (add to above)

**Implementation:**
```python
async def retry_evidence_creation(
    case_id: str,
    llm_result: dict,  # Serialized LLM output
    content_ref: str,
    content_hash: str,
    retry_count: int,
    max_retries: int = 5
):
    """Retry DB insert with exponential backoff"""

    if retry_count >= max_retries:
        logger.critical(
            f"Max retries reached for evidence creation. "
            f"Case: {case_id}, File: {content_ref}"
        )
        await alerting.critical(
            "Evidence creation failed permanently",
            details={"case_id": case_id, "content_ref": content_ref}
        )
        return

    try:
        case = await case_service.get(case_id)

        # Check if already exists (idempotency)
        existing = await evidence_repo.find_by_content_hash(case_id, content_hash)
        if existing:
            logger.info(f"Evidence already exists: {content_hash}")
            return

        # Retry insert
        evidence = Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            case_id=case_id,
            content_ref=content_ref,
            content_hash=content_hash,
            **llm_result
        )
        await evidence_repo.create(evidence)
        logger.info(f"Retry successful: {evidence.evidence_id}")

    except DBError as e:
        logger.warning(f"Retry {retry_count + 1} failed: {e}")
        delay = 2 ** retry_count * 10  # 10s, 20s, 40s, 80s, 160s
        await job_queue.enqueue_delayed(
            "retry_evidence_creation",
            delay_seconds=delay,
            case_id=case_id,
            llm_result=llm_result,
            content_ref=content_ref,
            content_hash=content_hash,
            retry_count=retry_count + 1,
            max_retries=max_retries
        )
```

**Testing:**
- [ ] Test successful retry after DB failure
- [ ] Test idempotency (duplicate check via content_hash)
- [ ] Test max retries triggers alert
- [ ] Test exponential backoff

---

#### Task 7.4: Update Evidence Creation with Error Handling

**File:** `faultmaven/core/investigation/milestone_engine.py`

**Changes:**
```python
async def process_turn_with_attachment(
    case_id: str,
    user_message: str,
    file: UploadFile
) -> TurnResponse:
    """Process turn with file, handling all failure modes"""

    content_ref = None
    content_hash = None

    try:
        # Step 1: Check for duplicate BEFORE upload
        content_hash = await compute_hash(file)
        existing = await evidence_repo.find_by_content_hash(case_id, content_hash)
        if existing:
            return TurnResponse(
                message="This file was already uploaded.",
                evidence_ref=existing.evidence_id,
                status="duplicate"
            )

        # Step 2: Upload with TTL metadata
        try:
            content_ref = await storage_service.upload(
                file,
                metadata={"ttl_hours": 24, "case_id": case_id}
            )
        except StorageError as e:
            logger.error(f"Upload failed: {e}")
            raise UserFacingError("Failed to upload file. Try again.")

        # Step 3: LLM analysis with timeout
        try:
            case = await case_service.get(case_id)
            llm_result = await llm_service.analyze(
                case, user_message, content_ref, timeout=30
            )
        except LLMTimeout:
            # Queue for retry (don't delete file)
            await job_queue.enqueue(
                "retry_evidence_analysis",
                case_id=case_id,
                content_ref=content_ref,
                content_hash=content_hash,
                user_message=user_message,
                retry_count=0
            )
            return TurnResponse(
                message="Analyzing... check back shortly.",
                status="analyzing"
            )
        except LLMError as e:
            # Cleanup file
            await storage_service.delete(content_ref)
            raise UserFacingError("Analysis failed. Try again.")

        # Step 4: Create evidence with DB retry
        try:
            evidence = Evidence(
                evidence_id=f"ev_{uuid4().hex[:12]}",
                case_id=case_id,
                content_ref=content_ref,
                content_hash=content_hash,
                **llm_result.model_dump()
            )
            await evidence_repo.create(evidence)
            return TurnResponse(
                message="Evidence saved.",
                evidence_ref=evidence.evidence_id,
                status="success"
            )
        except DBError:
            # Queue for retry (preserve LLM work)
            await job_queue.enqueue(
                "retry_evidence_creation",
                case_id=case_id,
                llm_result=llm_result.model_dump(),
                content_ref=content_ref,
                content_hash=content_hash,
                retry_count=0
            )
            return TurnResponse(
                message="Processing... will appear shortly.",
                status="processing"
            )

    except Exception as e:
        # Cleanup on unexpected error
        if content_ref:
            try:
                await storage_service.delete(content_ref)
            except:
                pass
        raise
```

**Testing:**
- [ ] Test upload failure → clean error
- [ ] Test LLM timeout → retry queued
- [ ] Test LLM error → file cleanup
- [ ] Test DB failure → retry queued with LLM result
- [ ] Test duplicate upload → early return

---

#### Task 7.5: Implement Storage Cleanup Job

**File:** `faultmaven/modules/agent/jobs/storage_cleanup.py` (NEW)

**Implementation:**
```python
async def cleanup_orphaned_files():
    """
    Daily job to delete files uploaded >24h ago with no evidence record.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=24)

    # List all files in evidence bucket
    files = await s3.list_objects(prefix="evidence/")

    orphaned_count = 0
    for file in files:
        uploaded_at_str = file.metadata.get("uploaded_at")
        if not uploaded_at_str:
            continue

        uploaded_at = datetime.fromisoformat(uploaded_at_str)
        if uploaded_at < cutoff:
            # Check if evidence record exists
            evidence_exists = await evidence_repo.exists_by_content_ref(
                file.key
            )

            if not evidence_exists:
                logger.info(f"Deleting orphaned file: {file.key}")
                await s3.delete(file.key)
                orphaned_count += 1

    # Metrics
    await metrics.gauge("evidence.orphaned_files_cleaned", orphaned_count)
    logger.info(f"Cleaned {orphaned_count} orphaned files")
```

**Scheduling:**
```python
# In celery beat schedule or cron
schedule = {
    'cleanup-orphaned-files': {
        'task': 'faultmaven.modules.agent.jobs.storage_cleanup.cleanup_orphaned_files',
        'schedule': crontab(hour=2, minute=0),  # Daily at 2 AM
    }
}
```

**Testing:**
- [ ] Test deletes files >24h old with no evidence
- [ ] Test preserves files with evidence records
- [ ] Test preserves recent files (<24h)
- [ ] Test metrics tracking

---

#### Task 7.6: Add Monitoring & Alerts

**File:** `faultmaven/infrastructure/monitoring/evidence_metrics.py` (NEW)

**Metrics to Track:**
```python
# Failure rates
metrics.counter("evidence.upload_failures")
metrics.counter("evidence.llm_timeouts")
metrics.counter("evidence.llm_errors")
metrics.counter("evidence.db_insert_failures")

# Retry metrics
metrics.counter("evidence.retry_attempts")
metrics.counter("evidence.retry_successes")
metrics.counter("evidence.retry_permanent_failures")

# Category fallback
metrics.counter("evidence.category_fallback")

# Storage cleanup
metrics.gauge("evidence.orphaned_files_cleaned")
```

**Alerts:**
```yaml
# LLM timeout rate > 5%
- alert: HighEvidenceLLMTimeoutRate
  expr: rate(evidence_llm_timeouts[5m]) / rate(evidence_llm_attempts[5m]) > 0.05
  severity: warning

# Permanent retry failures
- alert: EvidenceRetryPermanentFailures
  expr: increase(evidence_retry_permanent_failures[1h]) > 0
  severity: critical

# Orphaned file rate > 10%
- alert: HighOrphanedFileRate
  expr: evidence_orphaned_files_cleaned > 10
  severity: warning
```

**Testing:**
- [ ] Verify metrics recorded correctly
- [ ] Test alert triggers on failure scenarios
- [ ] Verify dashboards show failure rates

---

### Phase 8: Documentation & Rollout
**Duration:** 0.5 day
**Dependencies:** Phase 6 complete
**Risk:** Low

#### Task 7.1: Update API Documentation

**File:** `faultmaven/docs/api/README.md` or OpenAPI spec

**Changes:**
- Document new submission_classification in response
- Update evidence schema to show new categories
- Update source_type enum values
- Add analytics query examples

---

#### Task 7.2: Update Developer Guides

**Files:**
- `faultmaven/docs/development/evidence-handling.md`
- `faultmaven/docs/architecture/investigation-engine/README.md`

**Changes:**
- Update diagrams to show new flow (evidence created AFTER LLM)
- Remove references to UNCLASSIFIED
- Add examples of CONTEXTUAL_EVIDENCE vs REJECTED
- Document deduplication strategy

---

#### Task 7.3: Create Migration Runbook

**File:** `faultmaven/docs/operations/evidence-redesign-migration.md` (NEW)

**Contents:**
```markdown
# Evidence Redesign Migration Runbook

## Pre-Migration Checklist
- [ ] Backup production database
- [ ] Review UNCLASSIFIED evidence in production
- [ ] Decide migration strategy for UNCLASSIFIED

## Migration Steps
1. Run Alembic migration: `alembic upgrade head`
2. Verify schema changes: Check new columns, constraints, indexes
3. Verify data migration: All OTHER → CONTEXTUAL_EVIDENCE
4. Monitor error logs for 24 hours
5. Verify evidence creation in new cases

## Rollback Plan
1. Revert code deployment
2. Run Alembic downgrade: `alembic downgrade -1`
3. Restore database from backup if needed

## Monitoring
- Track evidence creation rate (should match previous rate)
- Track REJECTED evidence percentage (expect 5-15%)
- Monitor LLM classification accuracy
```

---

#### Task 7.4: Deploy to Staging

**Steps:**
1. Deploy schema migration to staging database
2. Deploy code changes to staging environment
3. Run full test suite on staging
4. Perform manual smoke testing (upload files, send chat, verify analytics)
5. Monitor for 24 hours

**Validation:**
- [ ] No UNCLASSIFIED evidence created
- [ ] REJECTED evidence created for rejected submissions
- [ ] Deduplication works
- [ ] Analytics queries return expected results
- [ ] No performance degradation

---

#### Task 7.5: Deploy to Production

**Steps:**
1. Schedule maintenance window (optional - migration is additive)
2. Deploy schema migration during low-traffic period
3. Deploy code changes
4. Monitor error logs, metrics, user reports
5. Verify evidence creation patterns match expectations

**Post-Deployment Monitoring (48 hours):**
- [ ] Evidence creation rate normal
- [ ] No errors in logs related to evidence
- [ ] User feedback positive (or no complaints)
- [ ] Analytics queries performant
- [ ] IRRELEVANT percentage within expected range (5-15%)

---

## Risk Mitigation

### High-Risk Areas

1. **UNCLASSIFIED Evidence Migration**
   - Risk: Existing UNCLASSIFIED evidence might be important
   - Mitigation: Manual review before migration, conservative defaults (mark as IRRELEVANT with note)

2. **Database Constraint Violations**
   - Risk: Unique constraints on (case_id, turn_number) might fail if multiple evidence per turn exist
   - Mitigation: Verify constraint before adding in migration, handle violations gracefully

3. **LLM Classification Accuracy**
   - Risk: LLM might misclassify submissions (mark relevant as IRRELEVANT)
   - Mitigation: Thorough prompt testing, allow users to "promote" IRRELEVANT → relevant category

---

## Success Criteria

**Phase 1-5 (Development):**
- [ ] All migrations run without errors
- [ ] All unit tests pass
- [ ] All integration tests pass
- [ ] Code review approved

**Phase 6 (Testing):**
- [ ] Manual testing checklist complete
- [ ] No regressions found
- [ ] Performance acceptable (< 10% slowdown)

**Phase 7 (Deployment):**
- [ ] Staging deployment successful
- [ ] Production deployment successful
- [ ] No post-deployment incidents
- [ ] Evidence classification accuracy > 90% (manual review of sample)

---

## Rollback Triggers

Initiate rollback if:
1. Database migration fails
2. Evidence creation rate drops > 50%
3. Error rate increases > 5x baseline
4. Critical evidence misclassified (relevant marked IRRELEVANT)
5. Performance degradation > 20%

---

## Timeline

| Phase | Duration | Start | End |
|-------|----------|-------|-----|
| Phase 1: Schema | 1 day | Day 1 | Day 1 |
| Phase 2: Models | 1 day | Day 2 | Day 2 |
| Phase 3: Prompts | 0.5 day | Day 3 AM | Day 3 PM |
| Phase 4: Logic | 2 days | Day 3 PM | Day 5 |
| Phase 5: API | 0.5 day | Day 5 PM | Day 6 AM |
| Phase 6: Testing | 1 day | Day 6 | Day 6 |
| Phase 7: Rollout | 0.5 day | Day 7 | Day 7 |

**Total: 6.5 days** (add buffer for unexpected issues)

---

## Open Questions / Decisions Needed

1. **UNCLASSIFIED Migration Strategy**: Manual review or automatic IRRELEVANT?
2. **Classification Accuracy Threshold**: What's acceptable? (Suggest 90%+)
3. **User Feedback Mechanism**: Should users be able to "promote" IRRELEVANT evidence?
4. **Analytics Dashboard**: Should we build UI to show acceptance rates?
5. **Staging Duration**: 24 hours sufficient or longer soak time needed?

---

**Implementation Team Assignment:**
- Backend Engineer: Phases 1-5 (schema, models, logic)
- QA Engineer: Phase 6 (testing, validation)
- DevOps Engineer: Phase 7 (deployment, monitoring)
- Tech Lead: Code review, risk assessment, go/no-go decision

**Status:** Ready to begin implementation
**Next Step:** Assign tasks to engineers, create tracking tickets
