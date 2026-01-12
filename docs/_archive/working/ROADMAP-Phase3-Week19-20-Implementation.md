# Phase 3 Week 19-20: Implementation Roadmap

**Related Documents**:
- [Design Document](./DESIGN-Phase3-Week19-20-High-Priority-Endpoints.md)
- [Architecture Diagrams](./ARCHITECTURE-Phase3-Week19-20-Diagrams.md)

**Status**: READY FOR IMPLEMENTATION
**Estimated Effort**: 15 days
**Team Size**: 1-2 developers

---

## Quick Start Checklist

Before starting implementation:

- [ ] Read design document completely
- [ ] Review architecture diagrams
- [ ] Verify local development environment is running
- [ ] Ensure PostgreSQL and ChromaDB are accessible
- [ ] Create feature branch: `feature/phase3-week19-20-high-priority-endpoints`
- [ ] Set up test database with sample data

---

## Implementation Phases

### Phase 1: Session Search (Days 1-3)

#### Day 1: Database Migration

**File**: `/home/swhouse/product/faultmaven/alembic/versions/xxx_add_session_fts_index.py`

```python
"""Add full-text search index to investigation_sessions

Revision ID: xxx
Revises: [CURRENT_HEAD]
Create Date: 2026-01-01
"""
from alembic import op

def upgrade():
    op.execute("""
        CREATE INDEX idx_session_fts
        ON investigation_sessions
        USING GIN(
            to_tsvector('english',
                session_goal || ' ' || COALESCE(findings_summary, '')
            )
        )
    """)

def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_session_fts")
```

**Tasks**:
1. Create migration file
2. Run migration: `alembic upgrade head`
3. Verify index creation: `\d+ investigation_sessions` in psql
4. Test query performance with EXPLAIN ANALYZE

**Validation**:
```sql
EXPLAIN ANALYZE
SELECT id, session_goal, findings_summary
FROM investigation_sessions
WHERE to_tsvector('english', session_goal || ' ' || COALESCE(findings_summary, ''))
      @@ plainto_tsquery('english', 'database connection')
ORDER BY ts_rank(
    to_tsvector('english', session_goal || ' ' || COALESCE(findings_summary, '')),
    plainto_tsquery('english', 'database connection')
) DESC;
```

Expected: Index scan, not sequential scan.

---

#### Day 2: Service Layer Implementation

**File**: `/home/swhouse/product/faultmaven/faultmaven/services/investigation_session_service.py`

Add method to `APIInvestigationSessionService`:

```python
async def search_sessions(
    self,
    query: str,
    organization_id: str,
    case_id: Optional[str] = None,
    status: Optional[SessionStatus] = None,
    limit: int = 20,
    offset: int = 0
) -> tuple[List[InvestigationSession], int]:
    """
    Full-text search across investigation sessions.

    Args:
        query: Search query string
        organization_id: Organization scope
        case_id: Optional case filter
        status: Optional status filter
        limit: Max results
        offset: Pagination offset

    Returns:
        Tuple of (matching sessions, total count)
    """
    # Build FTS query
    tsquery = func.plainto_tsquery('english', query)
    tsvector = func.to_tsvector(
        'english',
        InvestigationSession.session_goal + ' ' +
        func.coalesce(InvestigationSession.findings_summary, '')
    )

    # Base query
    stmt = (
        select(InvestigationSession)
        .where(
            InvestigationSession.organization_id == organization_id,
            tsvector.op('@@')(tsquery)
        )
    )

    # Apply filters
    if case_id:
        stmt = stmt.where(InvestigationSession.case_id == case_id)
    if status:
        stmt = stmt.where(InvestigationSession.status == status)

    # Add ranking
    rank = func.ts_rank(tsvector, tsquery).label('rank')
    stmt = stmt.add_columns(rank).order_by(rank.desc())

    # Count query
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = await self.session.scalar(count_stmt)

    # Paginated results
    stmt = stmt.limit(limit).offset(offset)
    result = await self.session.execute(stmt)
    sessions = [row[0] for row in result.all()]

    return sessions, total
```

**Tasks**:
1. Implement `search_sessions()` method
2. Handle edge cases (empty query, special characters)
3. Add input sanitization

**Unit Tests** (`tests/services/test_session_service.py`):

```python
class TestSessionSearch:
    async def test_search_sessions_by_query(self, session_service, db_session):
        """Test basic full-text search"""
        # Create test sessions
        session1 = await create_test_session(
            session_goal="Debug database connection timeout"
        )
        session2 = await create_test_session(
            session_goal="Investigate API latency issues"
        )

        # Search
        results, total = await session_service.search_sessions(
            query="database connection",
            organization_id="org_123"
        )

        assert total == 1
        assert results[0].id == session1.id

    async def test_search_sessions_with_case_filter(self, session_service):
        """Test search filtered by case_id"""
        results, total = await session_service.search_sessions(
            query="error",
            organization_id="org_123",
            case_id="case_456"
        )
        assert all(s.case_id == "case_456" for s in results)

    async def test_search_sessions_pagination(self, session_service):
        """Test offset/limit pagination"""
        # Create 30 sessions
        for i in range(30):
            await create_test_session(session_goal=f"Session {i} error")

        # Page 1
        page1, total = await session_service.search_sessions(
            query="error", organization_id="org_123", limit=10, offset=0
        )
        assert len(page1) == 10
        assert total == 30

        # Page 2
        page2, _ = await session_service.search_sessions(
            query="error", organization_id="org_123", limit=10, offset=10
        )
        assert len(page2) == 10
        assert page1[0].id != page2[0].id  # Different results

    async def test_search_sessions_relevance_scoring(self, session_service):
        """Test results ordered by relevance"""
        s1 = await create_test_session(
            session_goal="Database connection timeout"
        )
        s2 = await create_test_session(
            session_goal="API timeout",
            findings_summary="Database connection timeout occurred"
        )

        results, _ = await session_service.search_sessions(
            query="database connection timeout",
            organization_id="org_123"
        )

        # s1 should rank higher (all terms in goal)
        assert results[0].id == s1.id

    async def test_search_sessions_organization_isolation(self, session_service):
        """Test org isolation in search results"""
        await create_test_session(
            session_goal="Database error",
            organization_id="org_123"
        )
        await create_test_session(
            session_goal="Database error",
            organization_id="org_456"
        )

        results, total = await session_service.search_sessions(
            query="database",
            organization_id="org_123"
        )

        assert total == 1
        assert all(s.organization_id == "org_123" for s in results)

    # 3 more tests: empty results, special characters, status filter
```

Run tests: `pytest tests/services/test_session_service.py::TestSessionSearch -v`

---

#### Day 3: API Endpoint Implementation

**File**: `/home/swhouse/product/faultmaven/faultmaven/api/routes/sessions.py`

Add endpoint:

```python
from pydantic import BaseModel, Field
from typing import List, Optional

class SessionSearchResult(BaseModel):
    session_id: str
    case_id: str
    session_goal: str
    status: SessionStatus
    created_at: datetime
    relevance_score: float = Field(ge=0.0, le=1.0)

    @classmethod
    def from_domain(cls, session: InvestigationSession, score: float):
        return cls(
            session_id=session.id,
            case_id=session.case_id,
            session_goal=session.session_goal,
            status=session.status,
            created_at=session.created_at,
            relevance_score=score
        )

class SessionSearchResponse(BaseModel):
    results: List[SessionSearchResult]
    total: int
    limit: int
    offset: int
    query: str


@router.get("/search", response_model=SessionSearchResponse)
async def search_sessions(
    q: str = Query(..., min_length=1, max_length=500, description="Search query"),
    case_id: Optional[str] = Query(None),
    status: Optional[SessionStatus] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session_service: APIInvestigationSessionService = Depends(
        get_investigation_session_service
    ),
) -> SessionSearchResponse:
    """
    Full-text search across investigation sessions.

    Search sessions by keywords in session goal and findings.
    Results are ranked by relevance.

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>

    Query Parameters:
        q: Search query (required, 1-500 chars)
        case_id: Filter by specific case
        status: Filter by session status
        limit: Max results (1-100, default 20)
        offset: Pagination offset

    Returns:
        Matching sessions with relevance scores

    Example:
        GET /api/v1/sessions/search?q=database+timeout&limit=10
    """
    sessions, total = await session_service.search_sessions(
        query=q.strip(),
        organization_id=current_user.organization_id,
        case_id=case_id,
        status=status,
        limit=limit,
        offset=offset
    )

    results = [
        SessionSearchResult.from_domain(session, score=0.85)  # TODO: Extract actual score
        for session in sessions
    ]

    return SessionSearchResponse(
        results=results,
        total=total,
        limit=limit,
        offset=offset,
        query=q
    )
```

**Integration Tests** (`tests/integration/api/test_sessions_api.py`):

```python
class TestSessionSearchAPI:
    async def test_search_sessions_authenticated(self, client, auth_token):
        """Test successful search with auth"""
        response = await client.get(
            "/api/v1/sessions/search?q=database+error",
            headers={"Authorization": f"Bearer {auth_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "results" in data
        assert "total" in data
        assert data["query"] == "database error"

    async def test_search_sessions_unauthenticated(self, client):
        """Test 401 without auth token"""
        response = await client.get("/api/v1/sessions/search?q=test")
        assert response.status_code == 401

    async def test_search_sessions_invalid_query(self, client, auth_token):
        """Test 422 with invalid query params"""
        # Empty query
        response = await client.get(
            "/api/v1/sessions/search?q=",
            headers={"Authorization": f"Bearer {auth_token}"}
        )
        assert response.status_code == 422

        # Query too long
        long_query = "a" * 501
        response = await client.get(
            f"/api/v1/sessions/search?q={long_query}",
            headers={"Authorization": f"Bearer {auth_token}"}
        )
        assert response.status_code == 422

    async def test_search_sessions_pagination(self, client, auth_token):
        """Test pagination"""
        response = await client.get(
            "/api/v1/sessions/search?q=error&limit=5&offset=0",
            headers={"Authorization": f"Bearer {auth_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) <= 5
        assert data["limit"] == 5
        assert data["offset"] == 0

    async def test_search_sessions_response_schema(self, client, auth_token):
        """Test response matches OpenAPI schema"""
        response = await client.get(
            "/api/v1/sessions/search?q=test",
            headers={"Authorization": f"Bearer {auth_token}"}
        )
        data = response.json()

        # Validate structure
        assert isinstance(data["results"], list)
        assert isinstance(data["total"], int)
        assert isinstance(data["query"], str)

        if data["results"]:
            result = data["results"][0]
            assert "session_id" in result
            assert "case_id" in result
            assert "session_goal" in result
            assert "status" in result
            assert "created_at" in result
            assert "relevance_score" in result
            assert 0.0 <= result["relevance_score"] <= 1.0

    # 3 more tests: performance, concurrent requests, SQL injection
```

Run tests: `pytest tests/integration/api/test_sessions_api.py::TestSessionSearchAPI -v`

---

### Phase 2: Case Analytics (Days 4-7)

#### Day 4-5: Case Timeline Implementation

**File**: `/home/swhouse/product/faultmaven/faultmaven/services/case_service.py`

Add method:

```python
from typing import List, Optional, Dict, Any
from datetime import datetime

class TimelineEvent(BaseModel):
    event_id: str
    event_type: str
    timestamp: datetime
    actor_id: Optional[str]
    description: str
    metadata: Dict[str, Any]

TIMELINE_EVENT_TYPES = [
    "case_created", "case_updated", "case_status_changed",
    "case_assigned", "case_closed", "case_reopened",
    "session_created", "session_completed",
    "evidence_uploaded", "evidence_deleted"
]

async def get_case_timeline(
    self,
    case_id: str,
    organization_id: str,
    event_types: Optional[List[str]] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 100
) -> List[TimelineEvent]:
    """
    Generate chronological timeline of case events.

    Aggregates events from:
    - Cases table (lifecycle events)
    - Investigation sessions (session events)
    - Evidence artifacts (upload/delete events)

    Args:
        case_id: Case identifier
        organization_id: Organization scope
        event_types: Filter by event types
        start_date: Filter events after this date
        end_date: Filter events before this date
        limit: Max events to return

    Returns:
        List of timeline events sorted by timestamp DESC
    """
    events = []

    # 1. Get case
    case = await self.get_case(case_id, organization_id)
    if not case:
        raise NotFoundError("Case", case_id)

    # 2. Case creation event
    events.append(TimelineEvent(
        event_id=f"{case_id}_created",
        event_type="case_created",
        timestamp=case.created_at,
        actor_id=case.created_by,
        description=f"Case created: {case.title}",
        metadata={"severity": case.severity.value}
    ))

    # 3. Get sessions for this case
    sessions = await self.session_service.list_sessions(
        case_id=case_id,
        organization_id=organization_id,
        limit=1000  # Get all sessions
    )

    for session in sessions:
        events.append(TimelineEvent(
            event_id=f"{session.id}_created",
            event_type="session_created",
            timestamp=session.created_at,
            actor_id=session.user_id,
            description=f"Investigation session started",
            metadata={
                "session_id": session.id,
                "goal": session.session_goal
            }
        ))

        if session.status == SessionStatus.COMPLETED:
            events.append(TimelineEvent(
                event_id=f"{session.id}_completed",
                event_type="session_completed",
                timestamp=session.updated_at,
                actor_id=session.user_id,
                description=f"Session completed",
                metadata={
                    "session_id": session.id,
                    "findings": session.findings_summary
                }
            ))

    # 4. Get evidence for this case
    evidence_list = await self.evidence_service.list_evidence_by_case(
        case_id=case_id,
        organization_id=organization_id,
        limit=1000
    )

    for evidence in evidence_list:
        events.append(TimelineEvent(
            event_id=f"{evidence.id}_uploaded",
            event_type="evidence_uploaded",
            timestamp=evidence.uploaded_at,
            actor_id=evidence.uploaded_by,
            description=f"Evidence uploaded: {evidence.original_filename}",
            metadata={
                "evidence_id": evidence.id,
                "type": evidence.evidence_type.value
            }
        ))

    # 5. Sort by timestamp DESC
    events.sort(key=lambda e: e.timestamp, reverse=True)

    # 6. Apply filters
    if event_types:
        events = [e for e in events if e.event_type in event_types]
    if start_date:
        events = [e for e in events if e.timestamp >= start_date]
    if end_date:
        events = [e for e in events if e.timestamp <= end_date]

    # 7. Apply limit
    return events[:limit]
```

**Unit Tests** (`tests/services/test_case_service.py`):

```python
class TestCaseTimeline:
    async def test_get_case_timeline_all_events(self, case_service):
        """Test timeline with all event types"""
        # Create case with sessions and evidence
        case = await create_test_case()
        session = await create_test_session(case_id=case.id)
        evidence = await create_test_evidence(case_id=case.id)

        events = await case_service.get_case_timeline(
            case_id=case.id,
            organization_id="org_123"
        )

        # Should have: case_created, session_created, evidence_uploaded
        assert len(events) >= 3
        event_types = {e.event_type for e in events}
        assert "case_created" in event_types
        assert "session_created" in event_types
        assert "evidence_uploaded" in event_types

    async def test_get_case_timeline_filtered_events(self, case_service):
        """Test timeline with event type filter"""
        case = await create_test_case()
        await create_test_session(case_id=case.id)
        await create_test_evidence(case_id=case.id)

        events = await case_service.get_case_timeline(
            case_id=case.id,
            organization_id="org_123",
            event_types=["session_created", "evidence_uploaded"]
        )

        # Should NOT include case_created
        event_types = {e.event_type for e in events}
        assert "case_created" not in event_types
        assert "session_created" in event_types or "evidence_uploaded" in event_types

    async def test_get_case_timeline_chronological_order(self, case_service):
        """Test events sorted by timestamp DESC"""
        case = await create_test_case()
        events = await case_service.get_case_timeline(
            case_id=case.id,
            organization_id="org_123"
        )

        # Verify DESC order
        for i in range(len(events) - 1):
            assert events[i].timestamp >= events[i + 1].timestamp

    # 3 more tests: date range, empty case, limit
```

**API Endpoint** (`faultmaven/api/routes/cases.py`):

```python
@router.get("/{case_id}/timeline", response_model=List[TimelineEvent])
async def get_case_timeline(
    case_id: str,
    event_types: Optional[List[str]] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    current_user: AuthenticatedUser = Depends(get_current_user),
    case_service: APICaseService = Depends(get_api_case_service),
) -> List[TimelineEvent]:
    """
    Get chronological timeline of case events.

    Aggregates events from case lifecycle, sessions, and evidence.

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>

    Query Parameters:
        event_types: Filter by event types (optional)
        start_date: Filter events after this date
        end_date: Filter events before this date
        limit: Max events (1-500, default 100)

    Returns:
        List of timeline events sorted by timestamp DESC

    Example:
        GET /api/v1/cases/case_123/timeline?event_types=session_created,evidence_uploaded
    """
    events = await case_service.get_case_timeline(
        case_id=case_id,
        organization_id=current_user.organization_id,
        event_types=event_types,
        start_date=start_date,
        end_date=end_date,
        limit=limit
    )

    return events
```

**Integration Tests** (`tests/integration/api/test_cases_api.py`):

```python
class TestCaseTimelineAPI:
    async def test_get_timeline_success(self, client, auth_token):
        """Test successful timeline retrieval"""
        case = await create_test_case()
        response = await client.get(
            f"/api/v1/cases/{case.id}/timeline",
            headers={"Authorization": f"Bearer {auth_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    async def test_get_timeline_not_found(self, client, auth_token):
        """Test 404 for non-existent case"""
        response = await client.get(
            "/api/v1/cases/invalid_case/timeline",
            headers={"Authorization": f"Bearer {auth_token}"}
        )
        assert response.status_code == 404

    # 4 more tests: unauthorized, filters, schema, performance
```

---

#### Day 6-7: Case Trends Implementation

**File**: `/home/swhouse/product/faultmaven/faultmaven/services/case_service.py`

Add method:

```python
from sqlalchemy import func, extract, case as sql_case

class TrendDataPoint(BaseModel):
    timestamp: datetime
    count: int
    group_value: Optional[str] = None

class CaseTrendsResponse(BaseModel):
    start_date: datetime
    end_date: datetime
    interval: str
    total_cases: int
    data_points: List[TrendDataPoint]
    statistics: Dict[str, Any]

async def get_case_trends(
    self,
    organization_id: str,
    start_date: datetime,
    end_date: datetime,
    interval: str = "day",  # hour, day, week, month
    group_by: Optional[str] = None  # severity, status, assigned_to
) -> CaseTrendsResponse:
    """
    Get case trend analysis over time.

    Uses PostgreSQL date_trunc for time-series aggregation.

    Args:
        organization_id: Organization scope
        start_date: Trend start date
        end_date: Trend end date
        interval: Time bucket (hour, day, week, month)
        group_by: Optional grouping dimension

    Returns:
        Trend data points with counts
    """
    # Validate date range
    if end_date < start_date:
        raise HTTPException(422, "end_date must be after start_date")

    # Max 1 year range
    if (end_date - start_date).days > 365:
        raise HTTPException(422, "Date range cannot exceed 1 year")

    # Build query
    time_bucket = func.date_trunc(interval, Case.created_at).label('time_bucket')

    stmt = select(
        time_bucket,
        func.count().label('count')
    ).where(
        Case.organization_id == organization_id,
        Case.created_at >= start_date,
        Case.created_at <= end_date
    )

    # Add grouping
    if group_by == "severity":
        stmt = stmt.add_columns(Case.severity.label('group_value'))
        stmt = stmt.group_by(time_bucket, Case.severity)
    elif group_by == "status":
        stmt = stmt.add_columns(Case.status.label('group_value'))
        stmt = stmt.group_by(time_bucket, Case.status)
    elif group_by == "assigned_to":
        stmt = stmt.add_columns(Case.assigned_to.label('group_value'))
        stmt = stmt.group_by(time_bucket, Case.assigned_to)
    else:
        stmt = stmt.group_by(time_bucket)

    stmt = stmt.order_by(time_bucket)

    # Execute
    result = await self.session.execute(stmt)
    rows = result.all()

    # Format data points
    data_points = []
    total_cases = 0
    for row in rows:
        data_points.append(TrendDataPoint(
            timestamp=row.time_bucket,
            count=row.count,
            group_value=row.group_value if group_by else None
        ))
        total_cases += row.count

    # Calculate statistics
    statistics = {
        "average_cases_per_bucket": total_cases / max(len(data_points), 1),
        "peak_count": max((dp.count for dp in data_points), default=0),
        "min_count": min((dp.count for dp in data_points), default=0)
    }

    return CaseTrendsResponse(
        start_date=start_date,
        end_date=end_date,
        interval=interval,
        total_cases=total_cases,
        data_points=data_points,
        statistics=statistics
    )
```

**Unit Tests** (`tests/services/test_case_service.py`):

```python
class TestCaseTrends:
    async def test_get_trends_daily_interval(self, case_service):
        """Test trends with daily buckets"""
        # Create cases across multiple days
        for i in range(10):
            await create_test_case(
                created_at=datetime(2026, 1, i+1, 10, 0, 0)
            )

        trends = await case_service.get_case_trends(
            organization_id="org_123",
            start_date=datetime(2026, 1, 1),
            end_date=datetime(2026, 1, 31),
            interval="day"
        )

        assert trends.total_cases == 10
        assert len(trends.data_points) == 10

    async def test_get_trends_grouped_by_severity(self, case_service):
        """Test trends grouped by severity"""
        await create_test_case(severity=CaseSeverity.HIGH)
        await create_test_case(severity=CaseSeverity.LOW)

        trends = await case_service.get_case_trends(
            organization_id="org_123",
            start_date=datetime(2026, 1, 1),
            end_date=datetime(2026, 1, 31),
            interval="day",
            group_by="severity"
        )

        # Should have separate data points for each severity
        group_values = {dp.group_value for dp in trends.data_points}
        assert "high" in group_values or "low" in group_values

    # 4 more tests: weekly interval, date validation, empty results, org isolation
```

**API Endpoint** (`faultmaven/api/routes/cases.py`):

```python
@router.get("/trends", response_model=CaseTrendsResponse)
async def get_case_trends(
    start_date: datetime = Query(...),
    end_date: datetime = Query(...),
    interval: str = Query("day", regex="^(hour|day|week|month)$"),
    group_by: Optional[str] = Query(None, regex="^(severity|status|assigned_to)$"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    case_service: APICaseService = Depends(get_api_case_service),
) -> CaseTrendsResponse:
    """
    Get case trend analysis over time.

    Aggregates case creation trends with optional grouping.

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>

    Query Parameters:
        start_date: Trend start date (required)
        end_date: Trend end date (required)
        interval: Time bucket (hour, day, week, month)
        group_by: Grouping dimension (severity, status, assigned_to)

    Returns:
        Trend data points with counts and statistics

    Example:
        GET /api/v1/cases/trends?start_date=2025-12-01T00:00:00Z&end_date=2026-01-01T00:00:00Z&interval=day&group_by=severity
    """
    trends = await case_service.get_case_trends(
        organization_id=current_user.organization_id,
        start_date=start_date,
        end_date=end_date,
        interval=interval,
        group_by=group_by
    )

    return trends
```

**Integration Tests** (`tests/integration/api/test_cases_api.py`):

```python
class TestCaseTrendsAPI:
    async def test_get_trends_success(self, client, auth_token):
        """Test successful trends retrieval"""
        response = await client.get(
            "/api/v1/cases/trends?start_date=2025-12-01T00:00:00Z&end_date=2026-01-01T00:00:00Z&interval=day",
            headers={"Authorization": f"Bearer {auth_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "data_points" in data
        assert "total_cases" in data
        assert "statistics" in data

    # 4 more tests: invalid interval, date validation, schema, performance
```

---

### Phase 3: Knowledge Ingest (Days 8-11)

#### Day 8: Database Migration

**File**: `/home/swhouse/product/faultmaven/alembic/versions/xxx_add_knowledge_ingest_jobs.py`

```python
"""Add knowledge ingest jobs table

Revision ID: xxx
Revises: yyy
Create Date: 2026-01-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

def upgrade():
    op.create_table(
        'knowledge_ingest_jobs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('status', sa.String(50), nullable=False),
        sa.Column('total_documents', sa.Integer(), nullable=False),
        sa.Column('processed_documents', sa.Integer(), server_default='0'),
        sa.Column('failed_documents', sa.Integer(), server_default='0'),
        sa.Column('errors', JSON(), nullable=True),
        sa.Column('chunk_config', JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.String(36), nullable=True),
    )

    op.create_index('idx_ingest_jobs_status', 'knowledge_ingest_jobs', ['status'])
    op.create_index('idx_ingest_jobs_created_at', 'knowledge_ingest_jobs', ['created_at'])

def downgrade():
    op.drop_index('idx_ingest_jobs_created_at', 'knowledge_ingest_jobs')
    op.drop_index('idx_ingest_jobs_status', 'knowledge_ingest_jobs')
    op.drop_table('knowledge_ingest_jobs')
```

Run: `alembic upgrade head`

---

#### Day 9-10: Service Layer

**File**: `/home/swhouse/product/faultmaven/faultmaven/modules/knowledge/domain/services/knowledge_service.py`

Add methods:

```python
class IngestJob(BaseModel):
    id: str
    status: str  # queued, processing, completed, partial_failure, failed
    total_documents: int
    processed_documents: int
    failed_documents: int
    errors: List[Dict[str, str]] = []
    chunk_config: Dict[str, Any] = {}
    created_at: datetime
    completed_at: Optional[datetime] = None
    created_by: Optional[str] = None

async def create_ingest_job(self, total_documents: int, chunk_config: dict) -> IngestJob:
    """Create a new ingest job."""
    job_id = str(uuid.uuid4())
    job = IngestJob(
        id=job_id,
        status="queued",
        total_documents=total_documents,
        processed_documents=0,
        failed_documents=0,
        chunk_config=chunk_config,
        created_at=datetime.utcnow()
    )

    # Store in database
    stmt = insert(KnowledgeIngestJob).values(
        id=job.id,
        status=job.status,
        total_documents=job.total_documents,
        chunk_config=job.chunk_config,
        created_at=job.created_at
    )
    await self.session.execute(stmt)
    await self.session.commit()

    return job

async def queue_ingest_job(self, job_id: str, documents: List[dict], chunk_config: dict):
    """Queue ingest job for background processing."""
    # Use Celery or asyncio background task
    # For now, process synchronously (TODO: async worker)
    await self.process_ingest_job(job_id, documents, chunk_config)

async def process_ingest_job(self, job_id: str, documents: List[dict], chunk_config: dict):
    """Background task to process ingest job."""
    job = await self.get_ingest_job(job_id)
    job.status = "processing"
    await self.update_ingest_job(job)

    for idx, doc_data in enumerate(documents):
        try:
            # Chunk document
            if chunk_config.get("auto_chunk", True):
                chunks = self._chunk_text(
                    doc_data["content"],
                    chunk_size=chunk_config.get("chunk_size", 1000),
                    overlap=chunk_config.get("chunk_overlap", 200)
                )
            else:
                chunks = [doc_data["content"]]

            # Process each chunk
            for chunk_idx, chunk in enumerate(chunks):
                # Generate embedding
                embedding = await self.embedding_service.embed(chunk)

                # Store in vector DB
                await self.vector_store.add_document(
                    document_id=f"{job_id}_{idx}_{chunk_idx}",
                    content=chunk,
                    embedding=embedding,
                    metadata={
                        "title": doc_data["title"],
                        "document_type": doc_data["document_type"],
                        "category": doc_data.get("category"),
                        "tags": doc_data.get("tags", []),
                        "chunk_index": chunk_idx,
                        "batch_id": job_id
                    }
                )

            job.processed_documents += 1

        except Exception as e:
            logger.error(f"Failed to process document {idx}: {e}")
            job.failed_documents += 1
            job.errors.append({
                "document_index": idx,
                "error": str(e)
            })

        # Update progress every 10 docs
        if idx % 10 == 0:
            await self.update_ingest_job(job)

    # Finalize
    job.status = "completed" if job.failed_documents == 0 else "partial_failure"
    job.completed_at = datetime.utcnow()
    await self.update_ingest_job(job)

def _chunk_text(self, text: str, chunk_size: int, overlap: int) -> List[str]:
    """Simple text chunking."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start = end - overlap if end < len(text) else end
    return chunks
```

**Unit Tests** (10 tests): See design document.

---

#### Day 11: API Endpoint

**File**: `/home/swhouse/product/faultmaven/faultmaven/modules/knowledge/api/routes.py`

Add endpoint:

```python
@router.post("/ingest", status_code=202)
async def bulk_ingest_documents(
    request: KnowledgeIngestRequest,
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_admin)
) -> KnowledgeIngestResponse:
    """
    Bulk document ingestion with async processing.

    Accepts up to 100 documents for batch ingestion.
    Returns immediately with job ID for status tracking.

    Authentication:
        - JWT Bearer token: Authorization: Bearer <token>
        - Admin role required

    Body:
        documents: List of documents to ingest (1-100)
        auto_chunk: Enable automatic chunking (default: true)
        chunk_size: Chunk size in characters (500-5000, default: 1000)
        chunk_overlap: Overlap between chunks (0-1000, default: 200)

    Returns:
        202 Accepted with job ID and status URL

    Example:
        POST /api/v1/knowledge/ingest
        {
          "documents": [
            {
              "title": "DB Guide",
              "content": "...",
              "document_type": "troubleshooting_guide",
              "tags": ["database"]
            }
          ],
          "auto_chunk": true,
          "chunk_size": 1000
        }
    """
    # Validate batch size
    if len(request.documents) > 100:
        raise HTTPException(400, "Maximum 100 documents per batch")

    # Validate document types
    for idx, doc in enumerate(request.documents):
        if doc.document_type not in ALLOWED_DOCUMENT_TYPES:
            raise HTTPException(
                422,
                f"Document {idx}: Invalid document_type '{doc.document_type}'"
            )

    # Create job
    chunk_config = {
        "auto_chunk": request.auto_chunk,
        "chunk_size": request.chunk_size,
        "chunk_overlap": request.chunk_overlap
    }
    job = await knowledge_service.create_ingest_job(
        total_documents=len(request.documents),
        chunk_config=chunk_config
    )

    # Queue for processing
    await knowledge_service.queue_ingest_job(
        job_id=job.id,
        documents=[doc.dict() for doc in request.documents],
        chunk_config=chunk_config
    )

    return KnowledgeIngestResponse(
        job_id=job.id,
        status="queued",
        total_documents=len(request.documents),
        message=f"Ingest job {job.id} queued for processing",
        status_url=f"/api/v1/knowledge/jobs/{job.id}"
    )
```

**Integration Tests** (8 tests): See design document.

---

### Phase 4: Testing & Quality (Days 12-14)

#### Day 12: Integration Testing

Run full test suite:

```bash
# All tests
pytest tests/ -v

# Coverage report
pytest tests/ --cov=faultmaven --cov-report=html

# Open coverage report
open htmlcov/index.html
```

Verify:
- [ ] All 45+ new tests passing
- [ ] Overall coverage ≥71%
- [ ] No skipped tests (unless documented)

---

#### Day 13: Performance Testing

Create performance benchmarks:

**File**: `tests/performance/test_api_performance.py`

```python
import pytest
import time

class TestEndpointPerformance:
    async def test_session_search_performance(self, client, auth_token):
        """Session search: <500ms for 1000 sessions"""
        # Create 1000 sessions
        for i in range(1000):
            await create_test_session(session_goal=f"Session {i} error")

        start = time.time()
        response = await client.get(
            "/api/v1/sessions/search?q=error",
            headers={"Authorization": f"Bearer {auth_token}"}
        )
        duration_ms = (time.time() - start) * 1000

        assert response.status_code == 200
        assert duration_ms < 500, f"Search took {duration_ms}ms (target: <500ms)"

    async def test_case_timeline_performance(self, client, auth_token):
        """Timeline generation: <1s for 100 events"""
        case = await create_test_case()

        # Create 50 sessions + 50 evidence
        for i in range(50):
            await create_test_session(case_id=case.id)
            await create_test_evidence(case_id=case.id)

        start = time.time()
        response = await client.get(
            f"/api/v1/cases/{case.id}/timeline",
            headers={"Authorization": f"Bearer {auth_token}"}
        )
        duration_ms = (time.time() - start) * 1000

        assert response.status_code == 200
        assert duration_ms < 1000, f"Timeline took {duration_ms}ms (target: <1000ms)"

    # 2 more tests: trends, ingest
```

Run: `pytest tests/performance/ -v`

---

#### Day 14: Import Linter + Documentation

Run import-linter:

```bash
import-linter
```

Expected output: `0 contract violations found`

If violations found:
1. Review violation details
2. Fix imports according to contracts
3. Re-run until clean

Update OpenAPI documentation:

```bash
# Generate OpenAPI spec
python -m faultmaven.api.openapi > openapi.json

# Verify new endpoints appear
grep "sessions/search" openapi.json
grep "timeline" openapi.json
grep "trends" openapi.json
grep "ingest" openapi.json
```

---

### Phase 5: PR Preparation (Day 15)

#### Create Feature Branch

```bash
git checkout -b feature/phase3-week19-20-high-priority-endpoints
```

#### Commit Changes

```bash
# Add all changes
git add .

# Commit with descriptive message
git commit -m "$(cat <<'EOF'
feat: Implement Phase 3 Week 19-20 High Priority API Endpoints

Add 4 missing HIGH priority endpoints:

1. Session Search (GET /api/v1/sessions/search)
   - PostgreSQL full-text search
   - Relevance ranking
   - Case and status filters
   - Performance: <500ms

2. Case Timeline (GET /api/v1/cases/{id}/timeline)
   - Aggregates events from cases, sessions, evidence
   - Event type filtering
   - Chronological ordering
   - Performance: <1s

3. Case Trends (GET /api/v1/cases/trends)
   - Time-series aggregation (hour/day/week/month)
   - Group by severity/status/assigned_to
   - Redis caching (1h TTL)
   - Performance: <2s

4. Knowledge Ingest (POST /api/v1/knowledge/ingest)
   - Bulk document upload (max 100)
   - Async processing with job tracking
   - Automatic text chunking
   - Embedding pipeline integration

Database Changes:
- Add GIN index for session full-text search
- Add knowledge_ingest_jobs table

Testing:
- 45+ new tests (unit + integration + performance)
- Coverage maintained at 71%+
- All tests passing

Import Linter: 0 violations

Closes: Phase 3 Week 19-20 requirements
EOF
)"
```

#### Create Pull Request

```bash
# Push branch
git push origin feature/phase3-week19-20-high-priority-endpoints

# Create PR using GitHub CLI
gh pr create \
  --title "Phase 3 Week 19-20: High Priority API Endpoints" \
  --body "$(cat <<'EOF'
## Summary

Implements 4 missing HIGH priority endpoints for Phase 3 Week 19-20:

1. **Session Search** - Full-text search across investigation sessions
2. **Case Timeline** - Chronological event aggregation
3. **Case Trends** - Time-series trend analysis
4. **Knowledge Ingest** - Bulk document upload pipeline

## Changes

### Database Migrations
- [x] GIN index for session full-text search
- [x] knowledge_ingest_jobs table

### API Endpoints
- [x] GET /api/v1/sessions/search
- [x] GET /api/v1/cases/{id}/timeline
- [x] GET /api/v1/cases/trends
- [x] POST /api/v1/knowledge/ingest

### Service Layer
- [x] investigation_session_service.search_sessions()
- [x] case_service.get_case_timeline()
- [x] case_service.get_case_trends()
- [x] knowledge_service.create_ingest_job()
- [x] knowledge_service.process_ingest_job()

### Testing
- [x] 45+ tests passing (8 + 12 + 11 + 18)
- [x] Coverage ≥71% baseline maintained
- [x] Performance benchmarks passing
- [x] Import-linter: 0 violations

## Performance

| Endpoint | Target | Actual |
|----------|--------|--------|
| Session Search | <500ms | XXXms |
| Case Timeline | <1s | XXXms |
| Case Trends | <2s | XXXms |
| Knowledge Ingest | 202 <100ms | XXXms |

## Testing Checklist

- [ ] All unit tests pass
- [ ] All integration tests pass
- [ ] Performance benchmarks meet targets
- [ ] Import-linter shows 0 violations
- [ ] Manual testing via Swagger UI completed
- [ ] Database migrations tested (upgrade + downgrade)

## Documentation

- [x] Design document: docs/working/DESIGN-Phase3-Week19-20-High-Priority-Endpoints.md
- [x] Architecture diagrams: docs/working/ARCHITECTURE-Phase3-Week19-20-Diagrams.md
- [x] Implementation roadmap: docs/working/ROADMAP-Phase3-Week19-20-Implementation.md
- [x] OpenAPI spec updated

## Related

- Design: docs/working/DESIGN-Phase3-Week19-20-High-Priority-Endpoints.md
- Platform Evolution Strategy: docs/FAULTMAVEN_PLATFORM_EVOLUTION_STRATEGY.md
- Testing Standards: standards/TESTING_STANDARDS.md

## Review Focus

1. **Security**: Authentication, authorization, SQL injection prevention
2. **Performance**: Query optimization, index usage, caching strategy
3. **Testing**: Coverage, edge cases, error handling
4. **Code Quality**: Consistency with existing patterns, import-linter compliance

EOF
)"
```

---

## Success Criteria Verification

Before submitting PR, verify all criteria are met:

### Functional Requirements
- [ ] Session search returns relevant results
- [ ] Case timeline shows chronological events
- [ ] Case trends aggregates data correctly
- [ ] Knowledge ingest processes batches asynchronously

### Non-Functional Requirements
- [ ] All 45+ tests passing
- [ ] Test coverage ≥71%
- [ ] Import-linter: 0 violations
- [ ] Session search: <500ms
- [ ] Timeline: <1s
- [ ] Trends: <2s
- [ ] Ingest: 202 response <100ms

### Quality Gates
- [ ] OpenAPI spec updated
- [ ] Authentication/authorization working
- [ ] SQL injection prevention verified
- [ ] Organization isolation tested
- [ ] Error handling comprehensive
- [ ] Logging instrumented

---

## Troubleshooting

### Common Issues

**Issue: Migration fails**
```bash
# Check current revision
alembic current

# View migration history
alembic history

# Rollback and retry
alembic downgrade -1
alembic upgrade head
```

**Issue: Tests fail with "fixture not found"**
```bash
# Verify test fixtures are defined
grep -r "def.*fixture" tests/conftest.py
```

**Issue: Import-linter violations**
```bash
# View violations
import-linter --verbose

# Fix by moving imports or updating contracts
```

**Issue: Performance tests failing**
```bash
# Run with profiling
pytest tests/performance/ --profile

# Check database query execution
EXPLAIN ANALYZE <query>
```

---

## Post-Implementation

After PR is merged:

1. **Monitor Production**
   - Check error rates in logs
   - Monitor endpoint latency
   - Verify database index usage

2. **Update Documentation**
   - Move design docs from `docs/working/` to `docs/architecture/`
   - Update main README if needed
   - Add usage examples to wiki

3. **Celebrate**
   - Share success with team
   - Document lessons learned
   - Plan next phase

---

**End of Implementation Roadmap**
