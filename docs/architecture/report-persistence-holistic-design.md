# Report Persistence: Holistic System Design

**Date:** 2025-10-14
**Author:** Claude (Architectural Review)
**Purpose:** Holistic analysis of report persistence within FaultMaven's complete architecture

---

## 1. ARCHITECTURAL CONTEXT

### 1.1 FaultMaven System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│ FaultMaven Architecture (7-Layer Stack)                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. API Layer        → FastAPI endpoints, middleware            │
│  2. Service Layer    → Business logic orchestration             │
│  3. Agentic Layer    → AI agent, OODA loops, phase handlers    │
│  4. Core Domain      → Knowledge base, processing, investigation│
│  5. Infrastructure   → LLM, Redis, ChromaDB, Presidio           │
│  6. Models           → Pydantic schemas, interfaces             │
│  7. Container        → Dependency Injection (2464 lines)        │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 Existing Persistence Patterns

**Current Storage Infrastructure:**
```
Redis (Primary):
├── Sessions      → RedisSessionStore (transient, 24hr TTL)
├── Cases         → RedisCaseStore (30-day TTL)
├── User Data     → User tokens, auth state
└── Metrics       → Performance tracking

ChromaDB (Vector):
├── Knowledge Base → Document embeddings (BGE-M3)
└── Runbooks      → Already indexed for similarity (NEW)

NOT USED:
├── PostgreSQL    → Not in stack
├── MongoDB       → Not in stack
└── S3/MinIO      → Not in stack
```

**Key Observation:** FaultMaven uses **Redis + ChromaDB** exclusively for persistence. Adding a third storage system would break architectural consistency.

---

## 2. CROSS-CUTTING CONCERNS

### 2.1 Dependency Injection Container

**Current Container Structure** (container.py:2464 lines):
```python
class DIContainer:
    # Singleton instances
    _settings: Optional[Settings] = None
    _redis_client: Optional[redis.Redis] = None
    _case_store: Optional[ICaseStore] = None
    _session_store: Optional[ISessionStore] = None
    _vector_store: Optional[IVectorStore] = None
    _llm_router: Optional[LLMRouter] = None
    # ... 50+ more services
```

**QUESTION:** Where does `IReportStore` fit in this DI hierarchy?

**OPTIONS:**

**A. Coupled to CaseStore (Anti-Pattern)**
```python
class ICaseStore(ABC):
    @abstractmethod
    async def save_case_report(self, report: CaseReport) -> bool:
        """Reports are part of case lifecycle"""
```
❌ **Problems:**
- Violates Single Responsibility Principle
- Cases and reports have different lifecycles
- Reports outlive cases (90-day retention vs 30-day)
- Mixing transactional boundaries

**B. Separate ReportStore (Recommended)**
```python
class IReportStore(ABC):
    """Independent report persistence"""
    # Already designed this way ✅
```
✅ **Advantages:**
- Separation of concerns
- Independent lifecycle management
- Can scale reports separately from cases
- Clear interface boundaries

**DECISION:** Separate `IReportStore` with DI registration

---

### 2.2 Service Layer Integration

**Current Service Dependencies:**
```
CaseService
├── Depends on: ICaseStore
├── Manages: Case lifecycle, participants, messages
└── Does NOT manage: Report generation

ReportGenerationService (NEW)
├── Depends on: IReportStore, LLMRouter, RunbookKB
├── Manages: Report content generation
└── Called by: API layer when case is RESOLVED

AgentService
├── Depends on: CaseService, SessionService
├── Manages: OODA loop execution
└── Triggers: Case status → RESOLVED (which opens report flow)
```

**INTEGRATION POINT QUESTION:**
Should `CaseService` know about reports?

**OPTIONS:**

**A. CaseService manages reports directly**
```python
class CaseService:
    async def close_case_with_reports(self, case_id, reports):
        # Couples case lifecycle to report lifecycle
```
❌ **Problems:**
- Tight coupling
- CaseService becomes too large
- Violates separation of concerns

**B. Separate services, coordinated at API layer**
```python
# API endpoint coordinates multiple services
@router.post("/{case_id}/close")
async def close_case(...):
    reports = await report_service.get_latest_reports(case_id)
    await report_service.mark_linked_to_closure(case_id, reports)
    await case_service.update_status(case_id, CLOSED)
```
✅ **Advantages:**
- Loose coupling
- Each service has single responsibility
- API layer orchestrates cross-service workflows
- Easier to test independently

**DECISION:** Separate services, coordinated at API layer

---

### 2.3 Data Lifecycle Management

**CRITICAL QUESTION:** What happens to reports when a case is deleted?

**Current Case Deletion Flow:**
```python
# faultmaven/services/domain/case_service.py
async def delete_case(self, case_id: str) -> bool:
    """Delete case from store"""
    return await self.case_store.delete_case(case_id)
```

**OPTIONS:**

**A. Cascade Delete (Data Loss Risk)**
```python
async def delete_case(self, case_id: str) -> bool:
    await self.report_store.delete_case_reports(case_id)  # CASCADE
    return await self.case_store.delete_case(case_id)
```
❌ **Problems:**
- Loses valuable documentation
- No audit trail
- Cannot reference historical runbooks

**B. Soft Delete with Retention**
```python
async def delete_case(self, case_id: str) -> bool:
    # Mark case as deleted, but keep reports for 90 days
    await self.case_store.soft_delete_case(case_id)
    # Reports remain accessible via report_id
```
✅ **Advantages:**
- Audit trail preserved
- Runbooks remain in KB for similarity search
- Can restore accidentally deleted cases

**C. Orphan Reports (Current Design)**
```python
# Reports persist independently
# Can be retrieved by report_id even if case deleted
```
⚠️ **Trade-offs:**
- Reports become orphaned (case_id foreign key invalid)
- Need cleanup job to remove orphaned reports eventually
- Complexity in data integrity

**DECISION NEEDED:** Which deletion strategy aligns with FaultMaven's data governance?

---

### 2.4 Observability & Monitoring

**Current Observability Stack:**
```
- Opik: LLM call tracing
- Metrics Collector: Performance metrics
- Logging: Structured logging with context
- Tracing: @trace decorators on service methods
```

**QUESTIONS:**

1. **Should report generation be traced in Opik?**
   - LLM calls for report content → YES (already happens)
   - Storage operations → Should we trace Redis/ChromaDB latency?

2. **What metrics matter for reports?**
   ```python
   Metrics to track:
   - report_generation_duration_seconds
   - report_storage_duration_seconds
   - report_retrieval_duration_seconds
   - reports_per_case_count
   - report_regeneration_count
   - runbook_reuse_rate (from similarity recommendations)
   ```

3. **Error handling strategy?**
   - If report generation fails, what happens to case status?
   - If storage fails after generation, retry or fail?
   - Idempotency: Can we retry report generation safely?

**DECISION NEEDED:** Define observability requirements for report operations

---

### 2.5 Security & Privacy

**Current Security Layers:**
```
1. PII Redaction (Presidio)
   ├── Input sanitization
   └── Knowledge base sanitization

2. Access Control
   ├── User authentication (JWT tokens)
   └── Case ownership validation

3. API Rate Limiting
   └── Protection against abuse
```

**QUESTIONS:**

1. **Are report contents PII-redacted?**
   ```python
   # Current ReportGenerationService
   if self.pii_redactor:
       content = await self.pii_redactor.redact(content)  # ✅ YES
   ```

2. **Access control for report downloads?**
   ```python
   # Who can download reports?
   - Case owner only?
   - All case participants?
   - Anyone with report_id (security risk)?
   ```

3. **Report content in logs?**
   ```python
   logger.info(f"Report generated: {report.content}")  # ❌ DON'T LOG CONTENT
   logger.info(f"Report generated: {report.report_id}") # ✅ LOG ID ONLY
   ```

**DECISION NEEDED:** Define access control model for report downloads

---

### 2.6 Testing Strategy

**Current Test Coverage:** 1425+ tests, 71% coverage

**WHERE DO REPORT TESTS FIT?**

```
tests/
├── unit/
│   ├── models/
│   │   └── test_report_models.py              # NEW: Pydantic validation
│   ├── services/
│   │   └── test_report_generation_service.py  # NEW: Generation logic (DONE)
│   └── infrastructure/
│       └── test_redis_report_store.py         # NEW: Storage layer
│
├── integration/
│   └── test_report_workflow_e2e.py            # NEW: Full flow
│
├── api/
│   └── test_case_report_endpoints.py          # NEW: 5 endpoints
│
└── security/
    └── test_report_pii_redaction.py           # NEW: Privacy compliance
```

**QUESTION:** Do we need separate test fixtures for reports?
```python
@pytest.fixture
def mock_report_store():
    """Mock IReportStore for service tests"""

@pytest.fixture
def sample_case_with_reports():
    """Case with pre-generated reports for testing closure flow"""
```

**DECISION NEEDED:** Define test fixtures and mocking strategy

---

## 3. PERFORMANCE & SCALABILITY

### 3.1 Storage Capacity Analysis

**Assumptions:**
- 10,000 active cases per month
- 3 reports per case (incident_report, runbook, post_mortem)
- Average report size: 20KB markdown

**Storage Requirements:**
```
Per Month:
- Reports: 10,000 cases × 3 reports × 20KB = 600MB content
- Metadata: 30,000 reports × 200 bytes = 6MB metadata
- Total: ~606MB/month

Per Year:
- 7.2GB content + 72MB metadata = ~7.3GB/year

Redis Memory (Metadata Only):
- 72MB/year is negligible for Redis

ChromaDB Disk (Content):
- 7.2GB/year is manageable
- Consider compression (markdown compresses 3:1 → 2.4GB/year)
```

**CAPACITY VERDICT:** Current architecture can handle 10K cases/month easily

### 3.2 Query Performance

**Critical Query Paths:**

1. **GET /cases/{id}/reports (current only)**
   ```
   Redis lookup → HGETALL case:{id}:reports:current → O(1)
   For each report_id → HGETALL report:{id}:metadata → O(1)
   ChromaDB fetch → vector_store.query_by_embedding() → O(log n)

   Total: O(k × log n) where k = number of report types (max 3)
   Expected latency: <100ms
   ```

2. **GET /cases/{id}/reports (with history)**
   ```
   Redis range query → ZRANGE case:{id}:reports → O(log n + k)
   For each report_id → same as above

   Total: O(k × log n) where k = all versions (max 15)
   Expected latency: <300ms
   ```

3. **POST /cases/{id}/reports (generate + save)**
   ```
   LLM generation: 5-30 seconds (dominates)
   Storage: <500ms

   Total: Dominated by LLM, storage is negligible
   ```

**PERFORMANCE VERDICT:** Storage adds <500ms overhead, acceptable

### 3.3 Concurrent Access

**QUESTION:** What happens if two users try to generate reports simultaneously?

**SCENARIO:**
```
User A: POST /cases/123/reports at t=0
User B: POST /cases/123/reports at t=1

Both try to:
1. Mark previous version as not current
2. Save new version
3. Update indexes
```

**RISK:** Race condition in version management

**SOLUTIONS:**

**A. Optimistic Locking**
```python
# Check expected version before save
if case.report_generation_count != expected_count:
    raise ConcurrentModificationError()
```

**B. Redis Transactions (MULTI/EXEC)**
```python
pipe = redis.pipeline()
pipe.watch(f"case:{case_id}:reports:current")
# ... atomic operations
pipe.execute()
```

**C. Application-Level Locking**
```python
async with case_lock(case_id):
    # Only one report generation at a time per case
```

**DECISION NEEDED:** Choose concurrency control strategy

---

## 4. MIGRATION & ROLLOUT

### 4.1 Deployment Strategy

**QUESTION:** How do we roll out report persistence without breaking existing functionality?

**PHASES:**

**Phase 1: Feature Flag (Week 1)**
```python
# settings.py
class FeatureFlags:
    enable_report_persistence: bool = Field(
        default=False,
        env="FEATURE_REPORT_PERSISTENCE"
    )

# report_generation_service.py
if self.report_store and settings.feature_flags.enable_report_persistence:
    await self.report_store.save_report(report)
```

**Phase 2: Gradual Rollout (Week 2)**
```python
# Enable for 10% of cases
if hash(case_id) % 10 == 0:
    await self.report_store.save_report(report)
```

**Phase 3: Full Rollout (Week 3)**
```python
# Enable for all cases
await self.report_store.save_report(report)
```

**Phase 4: Deprecate In-Memory (Week 4)**
```python
# Remove fallback to returning reports without storage
```

### 4.2 Backward Compatibility

**QUESTION:** What happens to code that expects reports in API response?

**Current Behavior:**
```python
POST /cases/123/reports
→ Returns reports in response body immediately
```

**Should NOT Change:**
- Reports MUST still be returned in response
- Storage is additional, not replacement
- Allows clients to use reports without querying again

**DECISION:** Report persistence is **write-through** - return AND store

---

## 5. ALTERNATIVE APPROACHES CONSIDERED

### Option A: Reports as Case Attachments

```python
class Case:
    attachments: List[CaseAttachment]  # Reports are attachments

class CaseAttachment:
    type: AttachmentType  # Could be "report"
    content: str
```

**Pros:**
- Simple, no new persistence layer
- Reports move with case

**Cons:**
- Cases become huge (20KB+ per report)
- Inefficient for querying "all runbooks"
- Cannot do similarity search across cases

**VERDICT:** ❌ Rejected

### Option B: Reports in Knowledge Base Only

```python
# Store ALL reports in ChromaDB knowledge base
# Query via semantic search
```

**Pros:**
- Single storage location
- Automatic similarity search

**Cons:**
- Slow metadata queries (must search all docs)
- No fast "get reports for case" query
- Cannot filter by version, status efficiently

**VERDICT:** ❌ Rejected

### Option C: External Document Management System

**Pros:**
- Professional document storage
- Built-in versioning, access control

**Cons:**
- New infrastructure dependency
- Architectural inconsistency
- Operational complexity

**VERDICT:** ❌ Rejected (violates FaultMaven stack principles)

### Option D: Hybrid Redis + ChromaDB (CHOSEN)

**See earlier design sections**

**VERDICT:** ✅ **Selected** - Fits architecture, performant, testable

---

## 6. OPEN QUESTIONS FOR DECISION

### Critical Decisions Needed:

1. **Data Lifecycle**
   - [ ] Cascade delete reports when case deleted?
   - [ ] Or keep reports orphaned for 90 days?
   - [ ] Or soft-delete cases and keep reports?

2. **Access Control**
   - [ ] Report downloads: case owner only?
   - [ ] Or all case participants?
   - [ ] How to verify permissions?

3. **Concurrency Control**
   - [ ] Use Redis transactions (MULTI/EXEC)?
   - [ ] Or application-level locking?
   - [ ] Or optimistic locking with version checks?

4. **Observability**
   - [ ] Which metrics to track?
   - [ ] Should storage operations be traced in Opik?
   - [ ] Error alerting thresholds?

5. **Testing Strategy**
   - [ ] Integration tests with real Redis?
   - [ ] Or mock IReportStore in all tests?
   - [ ] E2E test coverage target?

6. **Deployment**
   - [ ] Feature flag rollout strategy?
   - [ ] Rollback plan if storage fails?
   - [ ] Monitoring during rollout?

---

## 7. RECOMMENDATIONS

### Immediate Next Steps:

1. **Answer Open Questions Above** (Decision meeting needed)

2. **Update Container Registration**
   ```python
   # container.py
   def get_report_store() -> IReportStore:
       if container._report_store is None:
           container._report_store = RedisReportStore(
               redis_client=get_redis_client(),
               vector_store=get_vector_store(),
               runbook_kb=get_runbook_kb()
           )
       return container._report_store
   ```

3. **API Dependencies Integration**
   ```python
   # api/v1/dependencies.py
   def get_report_store_dependency() -> IReportStore:
       return get_report_store()
   ```

4. **Service Layer Updates**
   - Update ReportGenerationService constructor in container
   - Inject IReportStore in all 4 API endpoints
   - Add error handling for storage failures

5. **Testing Infrastructure**
   - Create mock IReportStore fixture
   - Write integration tests with test Redis
   - Add E2E test for full report lifecycle

6. **Monitoring Setup**
   - Define metrics collection points
   - Add logging for storage operations
   - Create alerts for storage failures

### Long-Term Considerations:

- **Archival Strategy:** After 90 days, move old reports to cold storage?
- **Analytics:** Aggregate report data for insights (most common issues, resolution patterns)?
- **Export:** Bulk export of all reports for a case (ZIP file download)?
- **Search:** Full-text search across all reports?

---

## 8. CONCLUSION

Report persistence is **NOT an isolated feature** - it touches:

- ✅ Dependency injection container
- ✅ Service layer architecture
- ✅ API endpoint coordination
- ✅ Data lifecycle management
- ✅ Security & access control
- ✅ Observability & monitoring
- ✅ Testing strategy
- ✅ Deployment & rollout

**RECOMMENDATION:** Pause implementation until architectural decisions above are made by the team. Then implement holistically with proper integration across all layers.

**Current Status:**
- ✅ Interfaces designed (IReportStore)
- ✅ Implementation written (RedisReportStore)
- ⚠️ **Integration blocked** pending architectural decisions
