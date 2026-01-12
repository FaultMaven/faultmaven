# Phase 3 Week 19-20: Quick Start Guide

**For Developers**: Fast-track guide to implementing the 4 HIGH priority endpoints.

---

## TL;DR

Implement 4 endpoints:
1. `GET /api/v1/sessions/search` - Session search (Days 1-3)
2. `GET /api/v1/cases/{id}/timeline` - Case timeline (Days 4-5)
3. `GET /api/v1/cases/trends` - Case trends (Days 6-7)
4. `POST /api/v1/knowledge/ingest` - Bulk ingest (Days 8-11)

**Total**: 15 days, 61 tests, 0 import violations

---

## Before You Start

```bash
# 1. Read the design document
cat docs/working/DESIGN-Phase3-Week19-20-High-Priority-Endpoints.md

# 2. View architecture diagrams
cat docs/working/ARCHITECTURE-Phase3-Week19-20-Diagrams.md

# 3. Create feature branch
git checkout -b feature/phase3-week19-20-high-priority-endpoints

# 4. Verify environment
docker-compose up -d
alembic current
pytest tests/ --collect-only  # Should see ~4551 existing tests
```

---

## Implementation Checklist

### Session Search (Days 1-3)

**Day 1: Database**
```bash
# Create migration
alembic revision -m "Add session full-text search index"
# Edit file: alembic/versions/xxx_add_session_fts_index.py
# Add GIN index creation
alembic upgrade head
```

**Day 2: Service**
```python
# Edit: faultmaven/services/investigation_session_service.py
# Add: search_sessions() method
# Write 8 unit tests in tests/services/test_session_service.py
pytest tests/services/test_session_service.py::TestSessionSearch -v
```

**Day 3: API**
```python
# Edit: faultmaven/api/routes/sessions.py
# Add: GET /search endpoint
# Write 8 integration tests in tests/integration/api/test_sessions_api.py
pytest tests/integration/api/test_sessions_api.py::TestSessionSearchAPI -v
```

---

### Case Timeline (Days 4-5)

**Day 4: Service**
```python
# Edit: faultmaven/services/case_service.py
# Add: get_case_timeline() method
# Write 6 unit tests
pytest tests/services/test_case_service.py::TestCaseTimeline -v
```

**Day 5: API**
```python
# Edit: faultmaven/api/routes/cases.py
# Add: GET /{case_id}/timeline endpoint
# Write 6 integration tests
pytest tests/integration/api/test_cases_api.py::TestCaseTimelineAPI -v
```

---

### Case Trends (Days 6-7)

**Day 6: Service**
```python
# Edit: faultmaven/services/case_service.py
# Add: get_case_trends() method (with Redis caching)
# Write 6 unit tests
pytest tests/services/test_case_service.py::TestCaseTrends -v
```

**Day 7: API**
```python
# Edit: faultmaven/api/routes/cases.py
# Add: GET /trends endpoint
# Write 5 integration tests
pytest tests/integration/api/test_cases_api.py::TestCaseTrendsAPI -v
```

---

### Knowledge Ingest (Days 8-11)

**Day 8: Database**
```bash
# Create migration
alembic revision -m "Add knowledge ingest jobs table"
# Edit file: alembic/versions/xxx_add_knowledge_ingest_jobs.py
# Add table creation
alembic upgrade head
```

**Day 9-10: Service**
```python
# Edit: faultmaven/modules/knowledge/domain/services/knowledge_service.py
# Add: create_ingest_job(), queue_ingest_job(), process_ingest_job()
# Write 10 unit tests
pytest tests/modules/knowledge/test_knowledge_service.py::TestKnowledgeIngest -v
```

**Day 11: API**
```python
# Edit: faultmaven/modules/knowledge/api/routes.py
# Add: POST /ingest endpoint
# Write 8 integration tests
pytest tests/integration/api/test_knowledge_api.py::TestKnowledgeIngestAPI -v
```

---

## Testing & Quality (Days 12-14)

**Day 12: Integration Testing**
```bash
# Run all tests
pytest tests/ -v

# Check coverage
pytest tests/ --cov=faultmaven --cov-report=html
open htmlcov/index.html

# Verify coverage ≥71%
```

**Day 13: Performance Testing**
```bash
# Create performance tests
# Edit: tests/performance/test_api_performance.py
# Add 4 performance benchmarks

pytest tests/performance/ -v

# Expected:
# - Session search: <500ms
# - Case timeline: <1s
# - Case trends: <2s
# - Ingest 202: <100ms
```

**Day 14: Import Linter**
```bash
# Run import-linter
import-linter

# Expected: "0 contract violations found"
# If violations: fix imports and re-run
```

---

## PR Creation (Day 15)

```bash
# 1. Commit all changes
git add .
git commit -m "feat: Implement Phase 3 Week 19-20 High Priority Endpoints"

# 2. Push branch
git push origin feature/phase3-week19-20-high-priority-endpoints

# 3. Create PR
gh pr create \
  --title "Phase 3 Week 19-20: High Priority API Endpoints" \
  --body-file docs/working/PR-DESCRIPTION.md

# 4. Request reviews
gh pr edit --add-reviewer @tech-lead,@security-auditor
```

---

## File Locations Reference

### Design Documents
```
docs/working/
├── DESIGN-Phase3-Week19-20-High-Priority-Endpoints.md  # Main design
├── ARCHITECTURE-Phase3-Week19-20-Diagrams.md            # Diagrams
├── ROADMAP-Phase3-Week19-20-Implementation.md           # Day-by-day plan
├── PR-REVIEW-CHECKLIST-Phase3-Week19-20.md              # Review checklist
├── EXECUTIVE-SUMMARY-Phase3-Week19-20.md                # Executive summary
└── QUICKSTART-Phase3-Week19-20.md                       # This file
```

### Code Files (Modified/Created)
```
faultmaven/
├── alembic/versions/
│   ├── xxx_add_session_fts_index.py         # NEW
│   └── xxx_add_knowledge_ingest_jobs.py     # NEW
├── api/routes/
│   ├── sessions.py                          # MODIFIED
│   └── cases.py                             # MODIFIED
├── modules/knowledge/
│   ├── api/routes.py                        # MODIFIED
│   └── domain/services/knowledge_service.py # MODIFIED
├── services/
│   ├── investigation_session_service.py     # MODIFIED
│   └── case_service.py                      # MODIFIED
└── tests/
    ├── services/
    │   ├── test_session_service.py          # MODIFIED
    │   ├── test_case_service.py             # MODIFIED
    │   └── test_knowledge_service.py        # MODIFIED
    ├── integration/api/
    │   ├── test_sessions_api.py             # MODIFIED
    │   ├── test_cases_api.py                # MODIFIED
    │   └── test_knowledge_api.py            # MODIFIED
    └── performance/
        └── test_api_performance.py          # MODIFIED
```

---

## Common Commands

### Run specific test category
```bash
# Unit tests only
pytest tests/services/ -v

# Integration tests only
pytest tests/integration/ -v

# Performance tests only
pytest tests/performance/ -v

# Specific endpoint tests
pytest tests/integration/api/test_sessions_api.py -v
```

### Database operations
```bash
# Create migration
alembic revision -m "Description"

# Apply migrations
alembic upgrade head

# Rollback migration
alembic downgrade -1

# View current version
alembic current

# View migration history
alembic history
```

### Coverage analysis
```bash
# Generate HTML report
pytest tests/ --cov=faultmaven --cov-report=html

# Generate terminal report
pytest tests/ --cov=faultmaven --cov-report=term-missing

# Check specific module
pytest tests/ --cov=faultmaven.services.investigation_session_service
```

### Import linting
```bash
# Check violations
import-linter

# Verbose output
import-linter --verbose

# Show contract details
import-linter --show-timings
```

---

## Performance Optimization Tips

### Session Search
```python
# Use EXPLAIN ANALYZE to verify index usage
EXPLAIN ANALYZE
SELECT * FROM investigation_sessions
WHERE to_tsvector('english', session_goal) @@ plainto_tsquery('english', 'query');

# Should show: "Index Scan using idx_session_fts"
```

### Case Timeline
```python
# Fetch related data in parallel
async with asyncio.TaskGroup() as tg:
    task1 = tg.create_task(fetch_sessions())
    task2 = tg.create_task(fetch_evidence())
    task3 = tg.create_task(fetch_case())
```

### Case Trends
```python
# Use Redis caching
cache_key = f"trends:{org_id}:{start_date}:{end_date}:{interval}"
cached = await redis.get(cache_key)
if cached:
    return json.loads(cached)

# ... compute trends ...

await redis.setex(cache_key, 3600, json.dumps(result))  # 1h TTL
```

---

## Troubleshooting

### Migration fails
```bash
# Check current state
alembic current

# View pending migrations
alembic heads

# Force specific version
alembic stamp <revision>

# Rollback and retry
alembic downgrade -1
alembic upgrade head
```

### Tests fail with fixture errors
```bash
# Verify fixtures exist
grep -r "@pytest.fixture" tests/conftest.py

# Clear pytest cache
pytest --cache-clear

# Run with verbose fixture info
pytest tests/ -v --fixtures
```

### Import-linter violations
```bash
# View detailed violations
import-linter --verbose

# Check contract file
cat .import-linter.ini

# Common fix: move import to correct module
# Bad:  from faultmaven.api.routes import ...
# Good: from faultmaven.services import ...
```

### Performance tests fail
```bash
# Increase timeout for CI
pytest tests/performance/ --timeout=60

# Profile slow tests
pytest tests/performance/ --durations=10

# Run with timing breakdown
pytest tests/performance/ -v --benchmark
```

---

## Success Criteria

Before creating PR, verify:

- [ ] All 61 tests passing (30 unit + 27 integration + 4 performance)
- [ ] Coverage ≥71% (check htmlcov/index.html)
- [ ] Import-linter: 0 violations
- [ ] Performance targets met:
  - [ ] Session search <500ms
  - [ ] Timeline <1s
  - [ ] Trends <2s
  - [ ] Ingest 202 <100ms
- [ ] Manual testing in Swagger UI completed
- [ ] Database migrations tested (upgrade + downgrade)

---

## Getting Help

### Documentation
- **Design**: `docs/working/DESIGN-Phase3-Week19-20-High-Priority-Endpoints.md`
- **Roadmap**: `docs/working/ROADMAP-Phase3-Week19-20-Implementation.md`
- **Architecture**: `docs/working/ARCHITECTURE-Phase3-Week19-20-Diagrams.md`

### Code Examples
- See roadmap document for complete code examples
- Check existing endpoints for patterns:
  - Session endpoints: `faultmaven/api/routes/sessions.py`
  - Case endpoints: `faultmaven/api/routes/cases.py`
  - Knowledge endpoints: `faultmaven/modules/knowledge/api/routes.py`

### Questions?
- Review PR checklist: `docs/working/PR-REVIEW-CHECKLIST-Phase3-Week19-20.md`
- Ask in team chat: #backend-development
- Tag: @solutions-architect for design questions

---

**Good luck with implementation!**
