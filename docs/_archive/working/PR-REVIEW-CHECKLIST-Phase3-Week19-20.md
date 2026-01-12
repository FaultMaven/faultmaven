# PR Review Checklist: Phase 3 Week 19-20 High Priority Endpoints

**PR Title**: Phase 3 Week 19-20: High Priority API Endpoints
**Reviewers**: @tech-lead @backend-team @security-auditor

---

## Review Overview

This PR implements 4 HIGH priority API endpoints:
1. Session Search (GET /sessions/search)
2. Case Timeline (GET /cases/{id}/timeline)
3. Case Trends (GET /cases/trends)
4. Knowledge Ingest (POST /knowledge/ingest)

**Estimated Review Time**: 2-3 hours
**Priority**: HIGH (Phase 3 milestone)

---

## Pre-Review Checklist

Before starting detailed review, verify:

- [ ] PR description is complete and links to design document
- [ ] All CI/CD checks passing (tests, linting, coverage)
- [ ] Branch is up-to-date with main/develop
- [ ] No merge conflicts
- [ ] Commits are well-organized and descriptive

---

## Code Review Checklist

### 1. Database Migrations

**Files**:
- `alembic/versions/xxx_add_session_fts_index.py`
- `alembic/versions/xxx_add_knowledge_ingest_jobs.py`

#### Session FTS Index Migration
- [ ] Migration file follows naming convention
- [ ] `upgrade()` creates GIN index correctly
- [ ] `downgrade()` drops index cleanly
- [ ] Index name follows convention: `idx_session_fts`
- [ ] Index targets correct columns (session_goal, findings_summary)
- [ ] Migration tested locally (upgrade + downgrade)

#### Ingest Jobs Table Migration
- [ ] Table schema matches design document
- [ ] All columns have correct types
- [ ] Indexes created (status, created_at)
- [ ] Foreign key constraints correct (if any)
- [ ] Default values appropriate
- [ ] Migration tested locally

**SQL Quality**:
- [ ] No raw SQL injection risks
- [ ] Uses parameterized queries
- [ ] Follows PostgreSQL best practices

---

### 2. Service Layer Implementation

**Files**:
- `faultmaven/services/investigation_session_service.py`
- `faultmaven/services/case_service.py`
- `faultmaven/modules/knowledge/domain/services/knowledge_service.py`

#### Session Search Service
- [ ] `search_sessions()` method signature correct
- [ ] PostgreSQL full-text search query optimized
- [ ] Organization isolation enforced
- [ ] Pagination implemented (limit/offset)
- [ ] Relevance scoring working
- [ ] Filters applied correctly (case_id, status)
- [ ] Error handling comprehensive
- [ ] Type hints complete
- [ ] Docstring clear and accurate

**Performance Checks**:
- [ ] Uses GIN index (verify with EXPLAIN)
- [ ] No N+1 query issues
- [ ] Query execution <500ms (check benchmarks)

#### Case Timeline Service
- [ ] `get_case_timeline()` method signature correct
- [ ] Aggregates from multiple tables (cases, sessions, evidence)
- [ ] Events sorted chronologically (DESC)
- [ ] Event type filtering works
- [ ] Date range filtering works
- [ ] Limit applied correctly
- [ ] Organization isolation enforced
- [ ] No duplicate events
- [ ] Performance <1s (check benchmarks)

#### Case Trends Service
- [ ] `get_case_trends()` method signature correct
- [ ] Uses `date_trunc()` for time buckets
- [ ] Supports all intervals (hour, day, week, month)
- [ ] Grouping dimensions work (severity, status, assigned_to)
- [ ] Date range validation (max 1 year)
- [ ] Statistics calculated correctly
- [ ] Caching strategy implemented (Redis, 1h TTL)
- [ ] Performance <2s (check benchmarks)

#### Knowledge Ingest Service
- [ ] `create_ingest_job()` creates job correctly
- [ ] `queue_ingest_job()` queues for background processing
- [ ] `process_ingest_job()` handles async processing
- [ ] Text chunking algorithm correct
- [ ] Embedding generation working
- [ ] Vector store integration correct
- [ ] Progress tracking updates
- [ ] Error collection and reporting
- [ ] Job status transitions correct (queued → processing → completed)
- [ ] Partial failures handled gracefully

---

### 3. API Endpoints

**Files**:
- `faultmaven/api/routes/sessions.py`
- `faultmaven/api/routes/cases.py`
- `faultmaven/modules/knowledge/api/routes.py`

#### Session Search Endpoint
- [ ] Route path: `GET /api/v1/sessions/search`
- [ ] Query parameters validated (q, case_id, status, limit, offset)
- [ ] Query length limits enforced (1-500 chars)
- [ ] JWT authentication required
- [ ] Organization scoped to current user
- [ ] Response model correct (SessionSearchResponse)
- [ ] HTTP status codes appropriate (200, 401, 422)
- [ ] OpenAPI documentation complete
- [ ] Example requests in docstring

#### Case Timeline Endpoint
- [ ] Route path: `GET /api/v1/cases/{case_id}/timeline`
- [ ] Query parameters validated (event_types, start_date, end_date, limit)
- [ ] JWT authentication required
- [ ] Case ownership verified (404 if not accessible)
- [ ] Response model correct (List[TimelineEvent])
- [ ] HTTP status codes appropriate (200, 401, 404)
- [ ] OpenAPI documentation complete

#### Case Trends Endpoint
- [ ] Route path: `GET /api/v1/cases/trends`
- [ ] Query parameters validated (start_date, end_date, interval, group_by)
- [ ] Date range validation enforced
- [ ] JWT authentication required
- [ ] Organization scoped
- [ ] Response model correct (CaseTrendsResponse)
- [ ] HTTP status codes appropriate (200, 401, 422)
- [ ] Caching headers set correctly

#### Knowledge Ingest Endpoint
- [ ] Route path: `POST /api/v1/knowledge/ingest`
- [ ] Request body validated (documents, chunk config)
- [ ] Batch size limit enforced (<= 100 docs)
- [ ] Document type validation
- [ ] Admin role required
- [ ] Returns 202 Accepted (async pattern)
- [ ] Job ID returned in response
- [ ] Status URL provided
- [ ] HTTP status codes appropriate (202, 400, 401, 403, 422)

---

### 4. Request/Response Models

**Files**:
- `faultmaven/api/models.py` or inline models in routes

#### New Pydantic Models
- [ ] `SessionSearchResult` - all fields present
- [ ] `SessionSearchResponse` - pagination fields included
- [ ] `TimelineEvent` - event structure correct
- [ ] `TrendDataPoint` - timestamp and count fields
- [ ] `CaseTrendsResponse` - statistics included
- [ ] `KnowledgeIngestRequest` - validation rules correct
- [ ] `KnowledgeIngestResponse` - job info complete
- [ ] `IngestJob` - status enum defined

**Model Quality**:
- [ ] All fields have type hints
- [ ] Field descriptions provided
- [ ] Validation rules appropriate (min/max, regex)
- [ ] Default values sensible
- [ ] Examples provided in docstrings

---

### 5. Testing

**Files**:
- `tests/services/test_session_service.py`
- `tests/services/test_case_service.py`
- `tests/modules/knowledge/test_knowledge_service.py`
- `tests/integration/api/test_sessions_api.py`
- `tests/integration/api/test_cases_api.py`
- `tests/integration/api/test_knowledge_api.py`
- `tests/performance/test_api_performance.py`

#### Unit Tests (30 tests)
- [ ] Session search: 8 tests
  - [ ] Basic search
  - [ ] Case filter
  - [ ] Status filter
  - [ ] Pagination
  - [ ] Relevance scoring
  - [ ] Empty results
  - [ ] Special characters
  - [ ] Org isolation
- [ ] Case timeline: 6 tests
  - [ ] All events
  - [ ] Event type filter
  - [ ] Date range filter
  - [ ] Chronological order
  - [ ] Empty case
  - [ ] Limit
- [ ] Case trends: 6 tests
  - [ ] Daily interval
  - [ ] Weekly interval
  - [ ] Group by severity
  - [ ] Date validation
  - [ ] Empty results
  - [ ] Org isolation
- [ ] Knowledge ingest: 10 tests
  - [ ] Create job
  - [ ] Queue job
  - [ ] Process success
  - [ ] Partial failure
  - [ ] Auto chunking
  - [ ] No chunking
  - [ ] Embeddings
  - [ ] Progress tracking
  - [ ] Error handling
  - [ ] Job status

#### Integration Tests (27 tests)
- [ ] Session search API: 8 tests
  - [ ] Authenticated
  - [ ] Unauthenticated
  - [ ] Invalid query
  - [ ] Pagination
  - [ ] Response schema
  - [ ] Performance
  - [ ] Concurrent requests
  - [ ] SQL injection
- [ ] Case timeline API: 6 tests
  - [ ] Success
  - [ ] Not found
  - [ ] Unauthorized
  - [ ] Filters
  - [ ] Schema
  - [ ] Performance
- [ ] Case trends API: 5 tests
  - [ ] Success
  - [ ] Invalid interval
  - [ ] Date validation
  - [ ] Schema
  - [ ] Performance
- [ ] Knowledge ingest API: 8 tests
  - [ ] Success
  - [ ] Admin required
  - [ ] Document type validation
  - [ ] Batch size limit
  - [ ] 202 Accepted
  - [ ] Job status polling
  - [ ] Chunking config
  - [ ] Embedding pipeline

#### Performance Tests (4 tests)
- [ ] Session search <500ms
- [ ] Case timeline <1s
- [ ] Case trends <2s
- [ ] Knowledge ingest 202 <100ms

**Test Quality**:
- [ ] All tests have clear docstrings
- [ ] Fixtures used appropriately
- [ ] No hardcoded values (use factories)
- [ ] Async tests use `async def` and `await`
- [ ] Test isolation (no cross-test pollution)
- [ ] Edge cases covered
- [ ] Error cases tested

---

### 6. Security Review

#### Authentication & Authorization
- [ ] All endpoints require JWT token
- [ ] Admin-only endpoints check role (knowledge ingest)
- [ ] Organization isolation enforced everywhere
- [ ] No user can access other org's data

#### Input Validation
- [ ] Query parameters validated with Pydantic
- [ ] Query length limits enforced
- [ ] Batch size limits enforced
- [ ] Date range limits enforced
- [ ] SQL injection prevention (parameterized queries)
- [ ] No arbitrary code execution risks

#### SQL Injection Prevention
- [ ] All queries use SQLAlchemy ORM or parameterized queries
- [ ] No raw string concatenation in SQL
- [ ] User input never directly inserted into queries
- [ ] Full-text search uses `plainto_tsquery` (safe)

**Manual SQL Injection Test**:
```bash
# Test session search with injection attempt
curl "http://localhost:8000/api/v1/sessions/search?q='; DROP TABLE sessions; --" \
  -H "Authorization: Bearer <token>"

# Should return 422 or sanitized results, NOT execute SQL
```

#### Rate Limiting
- [ ] Rate limits applied to resource-intensive endpoints
- [ ] Session search: 10 req/min per user
- [ ] Knowledge ingest: 5 req/hour

#### Data Privacy
- [ ] No PII exposed in logs
- [ ] Timeline events redact sensitive data
- [ ] Search results don't leak cross-org data

---

### 7. Performance Review

#### Database Indexes
- [ ] GIN index on investigation_sessions used
- [ ] EXPLAIN ANALYZE shows index scan (not seq scan)
- [ ] Timeline queries optimized
- [ ] Trends queries use indexes on created_at

**Manual Verification**:
```sql
EXPLAIN ANALYZE
SELECT ...
FROM investigation_sessions
WHERE to_tsvector(...) @@ plainto_tsquery(...);
-- Should show: "Index Scan using idx_session_fts"
```

#### Query Optimization
- [ ] No N+1 queries
- [ ] Joins minimized
- [ ] Pagination implemented
- [ ] Limit clauses prevent unbounded results

#### Caching Strategy
- [ ] Trends endpoint cached (Redis, 1h TTL)
- [ ] Cache key includes org_id
- [ ] Cache invalidation strategy documented

#### Performance Benchmarks
- [ ] Session search: ____ms (target: <500ms)
- [ ] Case timeline: ____ms (target: <1s)
- [ ] Case trends: ____ms (target: <2s)
- [ ] Ingest 202 response: ____ms (target: <100ms)

---

### 8. Code Quality

#### Style & Consistency
- [ ] Follows existing code patterns
- [ ] Consistent naming conventions
- [ ] Type hints on all functions
- [ ] Docstrings on all public methods
- [ ] No commented-out code
- [ ] No TODO comments without tickets

#### Import Organization
- [ ] Import-linter reports 0 violations
- [ ] Imports sorted (standard, third-party, local)
- [ ] No circular imports
- [ ] No wildcard imports (`from x import *`)

**Verify**:
```bash
import-linter
# Expected: "0 contract violations found"
```

#### Error Handling
- [ ] All exceptions handled appropriately
- [ ] HTTPException used for API errors
- [ ] Proper status codes (400, 401, 403, 404, 422, 500)
- [ ] Error messages user-friendly
- [ ] Sensitive info not leaked in errors

#### Logging
- [ ] Appropriate log levels (INFO, WARNING, ERROR)
- [ ] Structured logging (JSON format)
- [ ] No PII in logs
- [ ] Performance metrics logged

---

### 9. Documentation

#### Design Documents
- [ ] Design document complete and accurate
- [ ] Architecture diagrams clear
- [ ] Implementation roadmap detailed
- [ ] All files in `docs/working/`

#### API Documentation
- [ ] OpenAPI spec updated
- [ ] All endpoints documented
- [ ] Request/response examples provided
- [ ] Authentication requirements clear

#### Code Documentation
- [ ] Docstrings on all public APIs
- [ ] Complex logic explained with comments
- [ ] Migration files have clear descriptions

#### README Updates
- [ ] Updated if public API changed
- [ ] New features documented
- [ ] Migration instructions included

---

### 10. Testing Standards Compliance

Per [Testing Standards](../standards/TESTING_STANDARDS.md):

- [ ] All new code has tests
- [ ] Tests pass locally and in CI
- [ ] Coverage maintained at 71%+
- [ ] New endpoints have integration tests
- [ ] New business logic has unit tests
- [ ] Error conditions tested
- [ ] Security-critical changes have security tests
- [ ] Performance tests for critical paths
- [ ] No skipped tests without justification

---

## Non-Functional Review

### Scalability
- [ ] Endpoints handle high load (concurrent requests)
- [ ] Database queries scale to 10k+ records
- [ ] Pagination prevents memory issues
- [ ] Background jobs don't block API

### Observability
- [ ] Metrics instrumented (Prometheus)
- [ ] Logs include request IDs
- [ ] Tracing spans added to critical paths
- [ ] Error tracking configured

### Maintainability
- [ ] Code is readable and understandable
- [ ] Follows DRY principle
- [ ] Abstractions are appropriate
- [ ] Future extensibility considered

---

## PR Approval Criteria

### Must Have (Blocking)
- [ ] All tests passing (45+ tests)
- [ ] Coverage ≥71%
- [ ] Import-linter: 0 violations
- [ ] Security review passed
- [ ] Performance benchmarks met
- [ ] Database migrations tested

### Should Have (Non-Blocking)
- [ ] Documentation complete
- [ ] OpenAPI spec updated
- [ ] Performance optimizations applied
- [ ] Caching implemented (trends)

### Nice to Have (Optional)
- [ ] Additional test coverage (>75%)
- [ ] Performance exceeds targets
- [ ] Additional observability metrics

---

## Reviewer Sign-Off

### Code Review
**Reviewer**: _____________
**Date**: _____________
**Approved**: [ ] Yes [ ] No
**Comments**:

### Security Review
**Reviewer**: _____________
**Date**: _____________
**Approved**: [ ] Yes [ ] No
**Comments**:

### Performance Review
**Reviewer**: _____________
**Date**: _____________
**Approved**: [ ] Yes [ ] No
**Comments**:

---

## Post-Approval Tasks

After PR is approved and merged:

- [ ] Monitor production for errors (first 24h)
- [ ] Verify performance metrics in production
- [ ] Update user-facing documentation
- [ ] Notify stakeholders of new features
- [ ] Archive design docs to `docs/architecture/`
- [ ] Close related tickets/issues
- [ ] Celebrate success with team

---

**End of PR Review Checklist**
