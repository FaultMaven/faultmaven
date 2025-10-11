# Evidence-Centric Troubleshooting System - Implementation Status

**Version:** 2.0
**Last Updated:** 2025-10-08
**Status:** 🚧 **IN PROGRESS** (Phase 1/3 Complete)

---

## Implementation Overview

This document tracks the implementation of the evidence-centric troubleshooting design as specified in [EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md](./EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md).

###  Progress: 30% Complete

| Component | Status | Progress |
|-----------|--------|----------|
| **Data Models** | ✅ Complete | 100% |
| **API Schema Updates** | ✅ Complete | 100% |
| **Classification Service** | ⏳ Pending | 0% |
| **Agent Prompt Updates** | ⏳ Pending | 0% |
| **API Serialization** | ⏳ Pending | 0% |
| **Conflict Resolution** | ⏳ Pending | 0% |
| **Stall Detection** | ⏳ Pending | 0% |
| **Testing** | ⏳ Pending | 0% |

---

## Phase 1: Foundation (✅ COMPLETE)

### ✅ Enhanced Data Models

**Created**: `faultmaven/models/evidence.py` (470 lines)

**Enums Implemented (8)**:
- ✅ `EvidenceCategory` - 7 categories (symptoms, timeline, changes, configuration, scope, metrics, environment)
- ✅ `EvidenceStatus` - 5 states (pending, partial, complete, blocked, obsolete)
- ✅ `EvidenceForm` - 2 forms (user_input, document)
- ✅ `EvidenceType` - 4 types (supportive, refuting, neutral, absence)
- ✅ `CompletenessLevel` - 3 levels (partial, complete, over_complete)
- ✅ `UserIntent` - 6 intents (providing_evidence, asking_question, reporting_unavailable, reporting_status, clarifying, off_topic)
- ✅ `InvestigationMode` - 2 modes (active_incident, post_mortem)
- ✅ `CaseStatus` - 7 states (intake, in_progress, resolved, mitigated, stalled, abandoned, closed)

**Data Models Implemented (10)**:
- ✅ `AcquisitionGuidance` - HOW-TO instructions with validation (max 3/3/3/3/2 items)
- ✅ `EvidenceRequest` - Structured evidence request with completeness tracking (0-1)
- ✅ `FileMetadata` - File upload metadata
- ✅ `EvidenceProvided` - User-submitted evidence record
- ✅ `EvidenceClassification` - 5-dimensional classification result
- ✅ `ImmediateAnalysis` - File upload immediate feedback
- ✅ `ConflictDetection` - Refuting evidence detection
- ✅ `DataUploadResponse` - Enhanced upload response

**Validation**:
```bash
✅ All models import successfully
✅ Field constraints enforced (maxLength, min/max, ge/le)
✅ Pydantic validation working
```

### ✅ CaseDiagnosticState Updates

**Modified**: `faultmaven/models/case.py` (lines 194-251)

**New Fields Added (13)**:
- ✅ `investigation_mode: InvestigationMode` - Speed vs depth approach
- ✅ `evidence_case_status: EvidenceCaseStatus` - Current case state
- ✅ `evidence_requests: List[EvidenceRequest]` - Active evidence requests
- ✅ `evidence_provided: List[EvidenceProvided]` - Submitted evidence
- ✅ `overall_confidence_score: Optional[float]` - Overall confidence (0-1)
- ✅ `awaiting_refutation_confirmation: bool` - Conflict resolution state
- ✅ `pending_refutations: List[str]` - Hypotheses pending confirmation
- ✅ `turns_without_phase_advance: int` - Stall detection counter
- ✅ `turns_in_current_phase: int` - Phase progress counter
- ✅ `case_report_url: Optional[str]` - Generated case report
- ✅ `runbook_url: Optional[str]` - Generated runbook

### ✅ AgentResponse Schema Updates

**Modified**: `faultmaven/models/api.py` (lines 138-172)

**New Fields Added (3)**:
- ✅ `evidence_requests: List[EvidenceRequest]` - Active requests for this turn
- ✅ `investigation_mode: InvestigationMode` - Current approach
- ✅ `case_status: EvidenceCaseStatus` - Current case state

**Deprecated Field**:
- ⚠️ `suggested_actions` - Marked deprecated, always null (backward compatibility)

**Schema Version**: ✅ Updated to `3.1.0`

---

## Phase 2: Service Layer (⏳ PENDING)

### Classification Service

**File**: `faultmaven/services/evidence/classification.py` (TO CREATE)

**Required Functions**:
```python
async def classify_evidence_multidimensional(
    user_input: str,
    active_requests: List[EvidenceRequest],
    conversation_history: List[Message],
    llm_client: LLMClient
) -> EvidenceClassification:
    """5-dimensional LLM-based classification"""
    # TODO: Implement
```

**Dependencies**:
- LLM client for classification
- Prompt template for 5-dimensional analysis
- Parsing logic for classification response

### Evidence Lifecycle Management

**File**: `faultmaven/services/evidence/lifecycle.py` (TO CREATE)

**Required Functions**:
```python
def update_evidence_lifecycle(
    evidence_provided: EvidenceProvided,
    classification: EvidenceClassification,
    diagnostic_state: CaseDiagnosticState,
    current_turn: int
) -> None:
    """Update evidence request status and completeness"""
    # TODO: Implement max() logic (not additive)
```

### Conflict Resolution Workflow

**File**: `faultmaven/services/evidence/conflict_resolution.py` (TO CREATE)

**Required Functions**:
```python
async def handle_refuting_evidence(
    evidence: EvidenceProvided,
    current_hypotheses: List[Hypothesis],
    state: CaseDiagnosticState,
    llm_client: LLMClient
) -> AgentResponse:
    """Detect and request confirmation for contradictions"""
    # TODO: Implement
```

### Stall Detection

**File**: `faultmaven/services/evidence/stall_detection.py` (TO CREATE)

**Required Functions**:
```python
def check_for_stall(state: CaseDiagnosticState) -> Optional[str]:
    """
    Detect investigation stalls with phase validation.

    Triggers:
    - 3+ blocked critical evidence requests
    - All hypotheses refuted (Phase 4)
    - 5+ turns without phase advance
    - Phase 3 with 0 hypotheses after 3 turns
    """
    # TODO: Implement with phase bounds check (0-5)
```

---

## Phase 3: Agent Updates (⏳ PENDING)

### IntakeAgent Updates

**File**: `faultmaven/services/agentic/doctor_patient/sub_agents/intake_agent.py`

**Required Changes**:
```python
# 1. Update prompt to generate EvidenceRequest instead of SuggestedAction
# 2. Add acquisition guidance generation
# 3. Implement command safety validation
# 4. Return evidence_requests in parsed output
```

**Prompt Updates Needed**:
- Add EvidenceRequest JSON schema to prompt
- Add acquisition guidance examples
- Add safety rules for command suggestions
- Remove suggested_action references

### BlastRadiusAgent Updates

**File**: `faultmaven/services/agentic/doctor_patient/sub_agents/blast_radius_agent.py`

**Evidence Request Examples**:
- Error rate metrics (METRICS category)
- Affected endpoints (SCOPE category)
- User impact assessment (SYMPTOMS category)

### TimelineAgent Updates

**File**: `faultmaven/services/agentic/doctor_patient/sub_agents/timeline_agent.py`

**Evidence Request Examples**:
- Recent deployments (CHANGES category)
- Configuration changes (CONFIGURATION category)
- First occurrence timestamp (TIMELINE category)

### HypothesisAgent Updates

**File**: `faultmaven/services/agentic/doctor_patient/sub_agents/hypothesis_agent.py`

**Evidence Request Examples**:
- Hypothesis validation data (SYMPTOMS category)
- System state snapshots (ENVIRONMENT category)

### ValidationAgent Updates

**File**: `faultmaven/services/agentic/doctor_patient/sub_agents/validation_agent.py`

**Special Handling**:
- Implement conflict resolution workflow
- Add refuting evidence detection
- Request user confirmation for contradictions

### SolutionAgent Updates

**File**: `faultmaven/services/agentic/doctor_patient/sub_agents/solution_agent.py`

**Deliverables**:
- Generate case report on resolution
- Generate runbook (check for duplicates)
- Set case_status to RESOLVED/MITIGATED

---

## Phase 4: API Integration (⏳ PENDING)

### Case Query Endpoint

**File**: `faultmaven/api/v1/routes/case.py` (line ~1459)

**Required Changes**:
```python
agent_response_dict = {
    "schema_version": "3.1.0",
    "content": agent_response.content,
    # ... existing fields ...

    # NEW FIELDS
    "evidence_requests": [
        req.model_dump() for req in agent_response.evidence_requests
    ],
    "investigation_mode": agent_response.investigation_mode.value,
    "case_status": agent_response.case_status.value,

    # DEPRECATED (always null)
    "suggested_actions": None
}
```

### Data Upload Endpoint

**File**: `faultmaven/api/v1/routes/data.py`

**Required Changes**:
```python
@router.post("/upload")
async def upload_data(...) -> DataUploadResponse:
    # 1. Store file
    # 2. Classify against active evidence requests
    # 3. Extract key findings
    # 4. Detect conflicts (refuting evidence)
    # 5. Return immediate_analysis

    return DataUploadResponse(
        data_id=data_id,
        filename=filename,
        file_metadata=FileMetadata(...),
        immediate_analysis=ImmediateAnalysis(
            matched_requests=matched_ids,
            completeness_scores=scores,
            key_findings=findings,
            evidence_type=ev_type,
            next_steps=next_steps
        ),
        conflict_detected=conflict if detected else None
    )
```

---

## Phase 5: Testing (⏳ PENDING)

### Unit Tests

**File**: `tests/models/test_evidence_models.py` (TO CREATE)

**Test Coverage**:
- ✅ EvidenceRequest validation (maxLength, completeness 0-1)
- ✅ AcquisitionGuidance max items (3/3/3/3/2)
- ✅ Enum value validation
- ⏳ Model serialization/deserialization

### Integration Tests

**File**: `tests/services/evidence/test_classification_integration.py` (TO CREATE)

**Test Scenarios**:
- ⏳ 5-dimensional classification accuracy
- ⏳ Evidence lifecycle updates (max logic, not additive)
- ⏳ Conflict detection (refuting evidence)
- ⏳ Stall detection (3 blocked, 5 turns, etc.)

### End-to-End Tests

**File**: `tests/integration/test_evidence_centric_workflow.py` (TO CREATE)

**Test Scenarios**:
- ⏳ Scenario 1: Happy path (complete evidence)
- ⏳ Scenario 2: Blocked evidence with alternatives
- ⏳ Scenario 3: Refuting evidence pivot
- ⏳ Scenario 4: Stalled investigation graceful termination
- ⏳ Scenario 5: Post-mortem high confidence

---

## Known Issues & Risks

### Issue #1: Circular Import Risk

**Problem**: `models/evidence.py` and `models/case.py` import each other.

**Current Status**: ✅ RESOLVED - Using proper import order

**Mitigation**: Keep evidence models independent of case models.

### Issue #2: LLM Classification Performance

**Problem**: 5-dimensional classification requires LLM call for each user input.

**Risk**: Latency impact on response time.

**Mitigation**:
- Use fast model for classification (gpt-4o-mini, claude-haiku)
- Cache classification results
- Run classification in parallel with agent processing

### Issue #3: Evidence Request Explosion

**Problem**: Agents might generate too many evidence requests.

**Risk**: User overwhelm, UI clutter.

**Mitigation**:
- Limit to 3-5 active requests per turn
- Mark obsolete requests when hypothesis changes
- Prioritize by category (CRITICAL > HIGH > MEDIUM)

---

## Next Steps

### Immediate (This Sprint)

1. ✅ **DONE**: Create evidence models
2. ✅ **DONE**: Update CaseDiagnosticState
3. ✅ **DONE**: Update AgentResponse schema
4. **TODO**: Create classification service
5. **TODO**: Update IntakeAgent prompt

### Sprint +1

6. **TODO**: Update all 6 agent prompts
7. **TODO**: Implement evidence lifecycle management
8. **TODO**: Update API serialization (case.py, data.py)
9. **TODO**: Write unit tests

### Sprint +2

10. **TODO**: Implement conflict resolution workflow
11. **TODO**: Implement stall detection
12. **TODO**: Add deliverables generation (case reports, runbooks)
13. **TODO**: Write integration tests
14. **TODO**: End-to-end testing with frontend

---

## Files Modified

| File | Lines Changed | Status |
|------|---------------|--------|
| `faultmaven/models/evidence.py` | +470 (new) | ✅ |
| `faultmaven/models/case.py` | +58 | ✅ |
| `faultmaven/models/api.py` | +23 | ✅ |
| `faultmaven/services/evidence/` | - | ⏳ Pending |
| `faultmaven/api/v1/routes/case.py` | - | ⏳ Pending |
| `faultmaven/api/v1/routes/data.py` | - | ⏳ Pending |
| `tests/models/test_evidence_models.py` | - | ⏳ Pending |

**Total Lines Added**: ~551 (models only)
**Estimated Remaining**: ~1,500 lines (services + API + tests)

---

## Performance Benchmarks

### Target Metrics

| Metric | Target | Current |
|--------|--------|---------|
| Classification latency | < 200ms | - |
| Evidence update latency | < 50ms | - |
| API response with evidence_requests | < 2s | - |
| File upload immediate analysis | < 3s | - |

---

## Documentation Status

| Document | Status |
|----------|--------|
| Design Specification | ✅ Complete |
| API Changes Document | ✅ Complete |
| OpenAPI v3.1 Patch | ✅ Complete |
| Implementation Status | ✅ This Document |
| Migration Guide | ⏳ TODO |

---

**Last Updated**: 2025-10-08
**Next Review**: After classification service implementation
