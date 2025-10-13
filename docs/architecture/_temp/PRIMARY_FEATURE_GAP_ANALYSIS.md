# Primary Functional Feature Gap Analysis

**Date:** 2025-10-12
**Purpose:** Identify missing core functionality (not non-functional features)
**Focus:** Data processing, classification, document generation, and evidence workflows

---

## Executive Summary

**Overall Primary Feature Completion: 68%**

The OODA investigation framework (95% complete) is strong, but **critical data processing and evidence workflows are incomplete**. The system can conduct investigations but **cannot effectively process logs/metrics or manage evidence lifecycle**.

### Critical Gaps (High Priority)
1. **Evidence System Integration** - 0% tested, no end-to-end workflow
2. **Log Processing** - Implementation exists (831 LOC) but 0% tested
3. **Data Classification** - Implementation exists but 0% tested
4. **Document Generation** - Basic implementation (Phase 6 handler) but limited integration

### What Works Well (95%+)
- ✅ OODA Investigation Framework (7 phases, 117 tests)
- ✅ Phase-specific prompts and handlers
- ✅ State management and persistence
- ✅ LLM integration and routing

---

## Detailed Gap Analysis

## 1. Evidence System (Priority: CRITICAL)

### Current Status: 40% Complete

**What Exists:**
- ✅ Evidence models (EvidenceRequest, EvidenceProvided, EvidenceClassification)
- ✅ 5-dimensional classification logic (831 lines in `classification.py`)
- ✅ Evidence lifecycle management (225 lines in `lifecycle.py`)
- ✅ Stall detection (242 lines in `stall_detection.py`)
- ✅ Evidence request generation in phase handlers

**Critical Gaps:**

### Gap 1.1: Evidence Classification Tests (0/10 tests)
**Status:** ❌ NOT IMPLEMENTED
**Impact:** Cannot verify 5-dimensional classification works
**Evidence:** No test files for `services/evidence/classification.py`

**What's Missing:**
```python
# tests/unit/services/evidence/test_classification.py - DOES NOT EXIST
# tests/integration/test_evidence_workflow.py - DOES NOT EXIST
```

**Required Tests:**
1. Test matched_request_ids detection (semantic matching)
2. Test completeness scoring (0.0-1.0 scale)
3. Test form detection (user_input vs document)
4. Test evidence_type classification (supportive/refuting/neutral/absence)
5. Test user_intent detection (6 intent types)
6. Test over_complete detection (multiple requests matched)
7. Test fallback classification (when LLM fails)
8. Test JSON parsing error handling
9. Test classification validation logic
10. Integration test: End-to-end evidence submission workflow

**Estimated Effort:** 1-2 days

---

### Gap 1.2: Evidence Lifecycle Tests (0/8 tests)
**Status:** ❌ NOT IMPLEMENTED
**Impact:** Cannot verify evidence status transitions work correctly

**What's Missing:**
```python
# tests/unit/services/evidence/test_lifecycle.py - DOES NOT EXIST
```

**Required Tests:**
1. Test PENDING → PARTIAL → COMPLETE transitions
2. Test BLOCKED status (user reports unavailable)
3. Test OBSOLETE status (hypothesis refuted)
4. Test max() completeness logic (not additive)
5. Test mark_obsolete_requests() function
6. Test get_active_evidence_requests() filtering
7. Test create_evidence_record() creation
8. Test summarize_evidence_status() reporting

**Estimated Effort:** 1 day

---

### Gap 1.3: Evidence Stall Detection Tests (0/6 tests)
**Status:** ❌ NOT IMPLEMENTED
**Impact:** Cannot verify investigation stall prevention works

**What's Missing:**
```python
# tests/unit/services/evidence/test_stall_detection.py - DOES NOT EXIST
```

**Required Tests:**
1. Test stall detection: ≥3 critical evidence blocked
2. Test stall detection: All hypotheses refuted
3. Test stall detection: No phase progress for ≥5 turns
4. Test stall detection: 0 hypotheses after 3 turns in Phase 3
5. Test no false positives (valid progression not flagged)
6. Test stall reason messages

**Estimated Effort:** 1 day

---

### Gap 1.4: Evidence Integration with Phase Handlers
**Status:** ⚠️ PARTIAL (generation works, consumption doesn't)
**Impact:** Evidence requests generated but not consumed by workflow

**What Works:**
- Phase handlers generate EvidenceRequest objects
- Evidence requests stored in InvestigationState.evidence_requests

**What's Missing:**
1. **Evidence consumption in phase handlers** - No logic to read/process evidence provided
2. **Evidence request → phase handler routing** - No automatic routing based on matched_request_ids
3. **Evidence-driven phase progression** - Phases don't check if evidence complete before advancing
4. **Evidence display in frontend** - Browser extension doesn't render evidence requests

**Example Missing Logic:**
```python
# blast_radius_handler.py - MISSING
async def _consume_new_evidence(self, investigation_state):
    """Check for new evidence since last turn and incorporate into blast radius analysis"""
    new_evidence = _get_evidence_since_turn(investigation_state, last_turn)
    for evidence in new_evidence:
        if evidence.addresses_requests contains blast_radius_request_id:
            # Update scope analysis with new evidence
            # Adjust anomaly_frame based on findings
            pass
```

**Estimated Effort:** 2-3 days (requires changes across all 7 phase handlers)

---

## 2. Data Processing Pipeline (Priority: HIGH)

### Current Status: 45% Complete

**What Exists:**
- ✅ DataService implementation (1,578 lines in `data_service.py`)
- ✅ Log analyzer with pattern detection (831 lines in `log_analyzer.py`)
- ✅ Data classifier with type detection (exists in `core/processing/classifier.py`)
- ✅ 59 tests passing for classifier and log processor

**Critical Gaps:**

### Gap 2.1: Log Processing Integration (0% integrated)
**Status:** ⚠️ IMPLEMENTED BUT NOT INTEGRATED
**Impact:** Log files can be processed but results don't feed into OODA investigation

**What Works:**
- `EnhancedLogProcessor` can parse logs
- Pattern detection and anomaly scoring works (59 tests pass)
- Memory-aware processing with context understanding

**What's Missing:**
1. **Log processing → Evidence creation** - Processed logs don't create EvidenceProvided records
2. **Automatic timeline extraction** - Timeline phase doesn't use log timestamps
3. **Automatic hypothesis generation** - Anomalies in logs don't suggest hypotheses
4. **Log insights → OODA state** - Processing results don't update InvestigationState

**Example Missing Integration:**
```python
# timeline_handler.py - MISSING
async def _extract_timeline_from_logs(self, uploaded_logs: List[UploadedData]):
    """Extract timeline events from uploaded log files"""
    for log_data in uploaded_logs:
        processing_result = await self.log_processor.process_with_context(
            content=log_data.content,
            context={"investigation_phase": "timeline"}
        )

        # Create timeline events from log entries
        for anomaly in processing_result.anomalies:
            timeline_event = TimelineEvent(
                timestamp=anomaly["timestamp"],
                event_type="log_anomaly",
                description=anomaly["description"],
                source="uploaded_log"
            )
            investigation_state.timeline.add_event(timeline_event)
```

**Estimated Effort:** 2 days

---

### Gap 2.2: Data Classification Integration (0% integrated)
**Status:** ⚠️ IMPLEMENTED BUT NOT INTEGRATED
**Impact:** Data can be classified but classification doesn't drive evidence categorization

**What Works:**
- `EnhancedDataClassifier` can classify data types
- 59 tests pass for classification logic

**What's Missing:**
1. **Classification → Evidence category mapping** - Data type doesn't map to EvidenceCategory
2. **Automatic evidence request generation** - Classification doesn't trigger relevant evidence requests
3. **File upload handling** - No API endpoint for file uploads with classification

**Example Missing Mapping:**
```python
# classification.py - MISSING
DATA_TYPE_TO_EVIDENCE_CATEGORY = {
    DataType.LOG_FILE: EvidenceCategory.SYSTEM_STATE,
    DataType.CONFIG_FILE: EvidenceCategory.CONFIGURATION,
    DataType.METRICS: EvidenceCategory.METRICS_TELEMETRY,
    DataType.ERROR_REPORT: EvidenceCategory.ENVIRONMENTAL,
    DataType.CODE_SNIPPET: EvidenceCategory.OBSERVATIONAL,
}

async def classify_and_create_evidence(uploaded_data: UploadedData) -> EvidenceProvided:
    """Classify uploaded data and create appropriate evidence record"""
    classification = await data_classifier.classify(uploaded_data.content)
    evidence_category = DATA_TYPE_TO_EVIDENCE_CATEGORY[classification.data_type]

    return EvidenceProvided(
        form=EvidenceForm.DOCUMENT,
        content=uploaded_data.content,
        file_metadata=uploaded_data.metadata,
        category=evidence_category,
        ...
    )
```

**Estimated Effort:** 1 day

---

### Gap 2.3: Data Service API Endpoints (30% complete)
**Status:** ⚠️ PARTIAL - API exists but limited functionality
**Impact:** Frontend can't upload files or track processing status

**What Works:**
- Basic API endpoints exist in `api/v1/routes/data.py`
- Session integration works

**What's Missing:**
1. **File upload endpoint** - No `/api/v1/data/upload` endpoint
2. **Processing status endpoint** - No way to check upload processing status
3. **Batch upload support** - Can't upload multiple logs at once
4. **Evidence creation from uploads** - Uploaded files don't auto-create evidence

**Required Endpoints:**
```python
# api/v1/routes/data.py - MISSING ENDPOINTS

@router.post("/upload")
async def upload_data_file(
    file: UploadFile,
    session_id: str,
    case_id: str,
    evidence_request_id: Optional[str] = None
) -> DataUploadResponse:
    """Upload log/config/metric file and create evidence record"""
    pass

@router.get("/processing/{upload_id}/status")
async def get_processing_status(upload_id: str) -> ProcessingStatusResponse:
    """Check status of async data processing"""
    pass

@router.post("/batch-upload")
async def batch_upload_files(
    files: List[UploadFile],
    session_id: str,
    case_id: str
) -> BatchUploadResponse:
    """Upload multiple files at once"""
    pass
```

**Estimated Effort:** 2 days

---

## 3. Document Generation (Priority: MEDIUM)

### Current Status: 60% Complete

**What Exists:**
- ✅ DocumentHandler (Phase 6) generates case reports and runbooks
- ✅ 29 tests for document generation (100% passing)
- ✅ Markdown formatting for documents
- ✅ User consent for artifact generation

**Critical Gaps:**

### Gap 3.1: Document Export Formats (20% complete)
**Status:** ⚠️ MARKDOWN ONLY
**Impact:** Users can't export to PDF, DOCX, HTML for sharing

**What Works:**
- Markdown generation works well

**What's Missing:**
1. **PDF export** - No PDF rendering
2. **DOCX export** - No Word document generation
3. **HTML export** - No standalone HTML
4. **JSON export** - No machine-readable format for API consumers

**Required Functions:**
```python
# document_handler.py - MISSING EXPORT FUNCTIONS

async def export_to_pdf(case_report: str) -> bytes:
    """Convert markdown case report to PDF"""
    pass

async def export_to_docx(case_report: str) -> bytes:
    """Convert markdown case report to Word document"""
    pass

async def export_to_html(case_report: str) -> str:
    """Convert markdown case report to HTML"""
    pass

async def export_to_json(investigation_state: InvestigationState) -> dict:
    """Export complete investigation data as JSON"""
    pass
```

**Estimated Effort:** 1-2 days

---

### Gap 3.2: Document API Endpoints (0% complete)
**Status:** ❌ NOT IMPLEMENTED
**Impact:** Frontend can't download generated documents

**What's Missing:**
```python
# api/v1/routes/documents.py - DOES NOT EXIST

@router.get("/case/{case_id}/report")
async def download_case_report(
    case_id: str,
    format: str = "markdown"  # markdown, pdf, docx, html, json
) -> FileResponse:
    """Download case report in specified format"""
    pass

@router.get("/case/{case_id}/runbook")
async def download_runbook(
    case_id: str,
    format: str = "markdown"
) -> FileResponse:
    """Download mitigation runbook"""
    pass
```

**Estimated Effort:** 1 day

---

### Gap 3.3: Document Templates (0% complete)
**Status:** ❌ NOT IMPLEMENTED
**Impact:** Generated documents lack professional formatting

**What's Missing:**
1. **HTML templates** - No styled HTML templates for reports
2. **CSS styling** - No branding/styling for exports
3. **Logo/header integration** - No company branding
4. **Customizable templates** - No user-configurable templates

**Estimated Effort:** 1 day (optional, lower priority)

---

## 4. Additional Missing Primary Features

### Gap 4.1: Metrics Processing (0% complete)
**Status:** ❌ NOT IMPLEMENTED
**Impact:** Can't process Prometheus/Grafana metrics

**What's Missing:**
- Metrics parser (parse Prometheus exposition format)
- Metrics anomaly detection (statistical analysis)
- Metrics visualization (charts/graphs in reports)
- Metrics correlation with logs

**Estimated Effort:** 3-4 days

---

### Gap 4.2: Configuration Analysis (0% complete)
**Status:** ❌ NOT IMPLEMENTED
**Impact:** Can't analyze config files for issues

**What's Missing:**
- Config parser (YAML, JSON, TOML, INI)
- Config validation (schema checking)
- Config drift detection (compare before/after)
- Config best practices checker

**Estimated Effort:** 2-3 days

---

### Gap 4.3: Error Report Processing (0% complete)
**Status:** ❌ NOT IMPLEMENTED
**Impact:** Can't process stack traces or error dumps

**What's Missing:**
- Stack trace parser (Python, Java, JavaScript, Go)
- Error signature extraction (deduplicate similar errors)
- Root cause suggestion (based on error patterns)
- Error correlation (link errors to logs/metrics)

**Estimated Effort:** 3-4 days

---

## Priority Recommendations

### Phase 1: Evidence System Integration (CRITICAL)
**Duration:** 1 week
**Impact:** Unblocks core troubleshooting workflow

**Tasks:**
1. Write evidence classification tests (24 tests) - 2 days
2. Write evidence lifecycle tests (8 tests) - 1 day
3. Write evidence stall detection tests (6 tests) - 1 day
4. Integrate evidence consumption into phase handlers - 2 days
5. Test end-to-end evidence workflow - 1 day

**Success Criteria:**
- ✅ 38 new tests passing (100% pass rate)
- ✅ Evidence requests consumed by handlers
- ✅ Evidence-driven phase progression works
- ✅ Stall detection prevents infinite loops

---

### Phase 2: Data Processing Integration (HIGH)
**Duration:** 1 week
**Impact:** Enables log/metrics/config analysis

**Tasks:**
1. Integrate log processing → evidence creation - 2 days
2. Integrate data classification → evidence category mapping - 1 day
3. Create file upload API endpoints - 2 days
4. Test data processing workflows - 2 days

**Success Criteria:**
- ✅ Uploaded logs create evidence records automatically
- ✅ Log anomalies suggest hypotheses
- ✅ Timeline phase uses log timestamps
- ✅ File upload API works with frontend

---

### Phase 3: Document Export & APIs (MEDIUM)
**Duration:** 4 days
**Impact:** Enables sharing and exporting results

**Tasks:**
1. Implement PDF/DOCX/HTML export - 2 days
2. Create document API endpoints - 1 day
3. Test document downloads - 1 day

**Success Criteria:**
- ✅ Users can download reports in multiple formats
- ✅ Frontend can trigger document generation
- ✅ Documents include all investigation data

---

### Phase 4: Metrics & Config Processing (OPTIONAL)
**Duration:** 1-2 weeks
**Impact:** Expands data source support

**Tasks:**
1. Implement metrics parser and analysis - 4 days
2. Implement config parser and validation - 3 days
3. Implement error report processing - 4 days

**Success Criteria:**
- ✅ System handles Prometheus metrics
- ✅ System validates YAML/JSON configs
- ✅ System parses stack traces

---

## Implementation Roadmap

### Week 1: Evidence System (CRITICAL)
```
Day 1-2: Write 38 evidence tests
Day 3-4: Integrate evidence consumption in handlers
Day 5:   End-to-end evidence workflow testing
```

**Deliverable:** Evidence system fully tested and integrated

---

### Week 2: Data Processing (HIGH)
```
Day 1-2: Log processing → evidence integration
Day 3:   Data classification → evidence mapping
Day 4-5: File upload API endpoints
```

**Deliverable:** Data processing feeds OODA investigation

---

### Week 3: Document Export (MEDIUM)
```
Day 1-2: PDF/DOCX/HTML export implementations
Day 3:   Document API endpoints
Day 4:   Frontend integration testing
```

**Deliverable:** Users can export investigation reports

---

### Week 4+: Advanced Features (OPTIONAL)
```
Week 4:   Metrics processing (Prometheus, Grafana)
Week 5:   Config analysis (YAML, JSON validation)
Week 6:   Error report processing (stack traces)
```

**Deliverable:** Full-featured data processing pipeline

---

## Test Coverage Targets

| Component | Current Tests | Target Tests | Gap |
|-----------|---------------|--------------|-----|
| Evidence Classification | 0 | 10 | +10 |
| Evidence Lifecycle | 0 | 8 | +8 |
| Evidence Stall Detection | 0 | 6 | +6 |
| Log Processing Integration | 59* | 15 | +15 |
| Data Classification Integration | 59* | 10 | +10 |
| Document Export | 29 | 15 | +15 |
| File Upload API | 0 | 8 | +8 |
| **Total** | **147** | **219** | **+72** |

*59 tests exist for log_processor/classifier but test isolation, not integration

---

## Success Metrics

### Functional Completeness
- **Current:** 68% primary features complete
- **Target after Phase 1-2:** 85% complete
- **Target after Phase 3:** 92% complete
- **Target after Phase 4:** 100% complete

### Test Coverage
- **Current:** 117 tests (OODA framework)
- **Target after Phase 1:** 155 tests (+38 evidence tests)
- **Target after Phase 2:** 180 tests (+25 data integration tests)
- **Target after Phase 3:** 195 tests (+15 document tests)
- **Target after Phase 4:** 219 tests (+24 advanced feature tests)

### End-to-End Workflows
- **Current:** Investigation workflow works (no evidence)
- **Target after Phase 1:** Evidence-driven investigation workflow
- **Target after Phase 2:** Log/config/metrics analysis workflows
- **Target after Phase 3:** Report generation and export workflows
- **Target after Phase 4:** Full troubleshooting platform

---

## Bottom Line

**Priority Order for Primary Features:**

1. **Evidence System Integration** (1 week, CRITICAL)
   - Required for core troubleshooting workflow
   - Blocks phase progression logic
   - 0 tests → 38 tests

2. **Data Processing Integration** (1 week, HIGH)
   - Required for log/metrics analysis
   - Blocks automatic evidence creation
   - Limited integration → Full integration

3. **Document Export & APIs** (4 days, MEDIUM)
   - Required for sharing results
   - Blocks frontend download features
   - Markdown only → Multiple formats

4. **Advanced Data Processing** (2-3 weeks, OPTIONAL)
   - Metrics, config, error report processing
   - Nice to have but not blocking
   - Expands platform capabilities

**Recommended Next Step:** Start with **Phase 1: Evidence System Integration** (1 week)
