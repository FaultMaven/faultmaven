# Phase 3 Week 19-20: Architecture Diagrams

**Related Design**: [DESIGN-Phase3-Week19-20-High-Priority-Endpoints.md](./DESIGN-Phase3-Week19-20-High-Priority-Endpoints.md)

---

## System Architecture Overview

```mermaid
graph TB
    subgraph "API Gateway"
        AG[FastAPI App]
    end

    subgraph "API Routes Layer"
        SR[Sessions Router<br/>/api/v1/sessions]
        CR[Cases Router<br/>/api/v1/cases]
        KR[Knowledge Router<br/>/api/v1/knowledge]
    end

    subgraph "Service Layer"
        SS[Session Service<br/>search_sessions()]
        CS[Case Service<br/>get_timeline()<br/>get_trends()]
        KS[Knowledge Service<br/>queue_ingest_job()<br/>process_ingest_job()]
    end

    subgraph "Data Layer"
        PG[(PostgreSQL<br/>Cases, Sessions,<br/>Evidence)]
        CHR[(ChromaDB<br/>Vector Store)]
        RD[(Redis<br/>Cache)]
    end

    subgraph "Background Jobs"
        BW[Celery Worker<br/>Ingest Processing]
    end

    AG --> SR
    AG --> CR
    AG --> KR

    SR --> SS
    CR --> CS
    KR --> KS

    SS --> PG
    CS --> PG
    CS --> RD
    KS --> PG
    KS --> CHR
    KS --> BW

    BW --> PG
    BW --> CHR

    style SR fill:#ffcccc
    style CR fill:#ffcccc
    style KR fill:#ffcccc
    style SS fill:#ccffcc
    style CS fill:#ccffcc
    style KS fill:#ccffcc
```

---

## Session Search Flow

```mermaid
sequenceDiagram
    participant Client
    participant API as Sessions API
    participant Service as Session Service
    participant DB as PostgreSQL
    participant Cache as Redis

    Client->>API: GET /sessions/search?q=database error
    API->>API: Validate JWT token
    API->>API: Validate query params

    API->>Service: search_sessions(query, filters)

    Service->>DB: Full-Text Search Query
    Note over DB: SELECT * FROM sessions<br/>WHERE to_tsvector(...) @@ plainto_tsquery('database error')<br/>ORDER BY ts_rank DESC

    DB-->>Service: Matching sessions with scores
    Service->>Service: Filter by organization_id
    Service->>Service: Apply pagination

    Service-->>API: SessionSearchResponse
    API-->>Client: 200 OK + Results

    Note over Client,DB: Performance: <500ms for 1000 sessions
```

---

## Case Timeline Flow

```mermaid
sequenceDiagram
    participant Client
    participant API as Cases API
    participant Service as Case Service
    participant DB as PostgreSQL

    Client->>API: GET /cases/{id}/timeline?event_types=session_created,evidence_uploaded

    API->>Service: get_case_timeline(case_id, filters)

    par Fetch Events in Parallel
        Service->>DB: Get case events
        Service->>DB: Get session events
        Service->>DB: Get evidence events
    end

    DB-->>Service: Case data
    DB-->>Service: Session data
    DB-->>Service: Evidence data

    Service->>Service: Aggregate all events
    Service->>Service: Sort by timestamp DESC
    Service->>Service: Filter by event_types
    Service->>Service: Apply limit

    Service-->>API: CaseTimelineResponse
    API-->>Client: 200 OK + Timeline

    Note over Client,DB: Performance: <1s for 100 events
```

---

## Case Trends Flow

```mermaid
sequenceDiagram
    participant Client
    participant API as Cases API
    participant Service as Case Service
    participant Cache as Redis
    participant DB as PostgreSQL

    Client->>API: GET /cases/trends?interval=day&group_by=severity

    API->>Service: get_case_trends(start, end, interval, group_by)

    Service->>Cache: Check cache key
    Cache-->>Service: MISS

    Service->>DB: Time-series aggregation query
    Note over DB: SELECT date_trunc('day', created_at),<br/>severity, COUNT(*)<br/>FROM cases<br/>GROUP BY date_trunc, severity

    DB-->>Service: Aggregated data points
    Service->>Service: Calculate statistics
    Service->>Cache: Store result (TTL 1h)

    Service-->>API: CaseTrendsResponse
    API-->>Client: 200 OK + Trends

    Note over Client,DB: Performance: <2s for 1 year<br/>Cached: <50ms
```

---

## Knowledge Ingest Flow (Async)

```mermaid
sequenceDiagram
    participant Client
    participant API as Knowledge API
    participant Service as Knowledge Service
    participant DB as PostgreSQL
    participant Queue as Job Queue
    participant Worker as Celery Worker
    participant Vector as ChromaDB

    Client->>API: POST /knowledge/ingest<br/>{documents: [...]}

    API->>API: Validate admin role
    API->>API: Validate batch size (<100)
    API->>API: Validate document types

    API->>Service: create_ingest_job(documents)
    Service->>DB: INSERT INTO knowledge_ingest_jobs
    DB-->>Service: job_id

    Service->>Queue: Queue job for processing
    Queue-->>Service: Queued

    Service-->>API: IngestJobResponse
    API-->>Client: 202 Accepted<br/>{job_id, status_url}

    Note over Client,Worker: Async Processing Begins

    Worker->>Queue: Poll for jobs
    Queue-->>Worker: job_id, documents

    loop For each document
        Worker->>Worker: Chunk text
        Worker->>Worker: Generate embeddings
        Worker->>Vector: Store document chunks
        Worker->>DB: UPDATE job progress
    end

    Worker->>DB: UPDATE job status=completed
    DB-->>Worker: Success

    Note over Client,Worker: Client polls status endpoint

    Client->>API: GET /knowledge/jobs/{job_id}
    API->>Service: get_job_status(job_id)
    Service->>DB: SELECT FROM knowledge_ingest_jobs
    DB-->>Service: Job status
    Service-->>API: JobStatusResponse
    API-->>Client: 200 OK<br/>{status: "completed", processed: 50/50}
```

---

## Database Schema Changes

```mermaid
erDiagram
    investigation_sessions ||--o{ cases : "belongs to"
    investigation_sessions {
        uuid id PK
        uuid case_id FK
        uuid organization_id FK
        text session_goal "FTS indexed"
        text findings_summary "FTS indexed"
        varchar status
        timestamp created_at
        timestamp updated_at
    }

    cases ||--o{ evidence_artifacts : "has many"
    cases ||--o{ investigation_sessions : "has many"
    cases {
        uuid id PK
        uuid organization_id FK
        varchar title
        text description
        varchar severity
        varchar status
        timestamp created_at "indexed for trends"
        timestamp updated_at
    }

    evidence_artifacts {
        uuid id PK
        uuid case_id FK
        uuid organization_id FK
        varchar original_filename
        varchar evidence_type
        timestamp uploaded_at "indexed for timeline"
    }

    knowledge_ingest_jobs {
        uuid id PK "NEW TABLE"
        varchar status "indexed"
        int total_documents
        int processed_documents
        int failed_documents
        json errors
        json chunk_config
        timestamp created_at "indexed"
        timestamp completed_at
        uuid created_by
    }
```

---

## New Indexes for Performance

```mermaid
graph LR
    subgraph "Session Search Optimization"
        I1[idx_session_fts<br/>GIN Index on tsvector]
        I2[idx_session_org_status<br/>Composite Index]
    end

    subgraph "Timeline Query Optimization"
        I3[idx_case_created_at<br/>Timestamp Index]
        I4[idx_session_case_created<br/>Composite Index]
        I5[idx_evidence_case_uploaded<br/>Composite Index]
    end

    subgraph "Trends Query Optimization"
        I6[idx_case_created_trends<br/>Multi-column Index]
    end

    subgraph "Ingest Job Tracking"
        I7[idx_ingest_jobs_status<br/>Status Index]
        I8[idx_ingest_jobs_created_at<br/>Timestamp Index]
    end

    style I1 fill:#ffffcc
    style I2 fill:#ffffcc
    style I3 fill:#ccffff
    style I4 fill:#ccffff
    style I5 fill:#ccffff
    style I6 fill:#ffccff
    style I7 fill:#ccffcc
    style I8 fill:#ccffcc
```

---

## Component Interaction Matrix

| Component | Session Search | Case Timeline | Case Trends | Knowledge Ingest |
|-----------|----------------|---------------|-------------|------------------|
| **API Routes** | `sessions.py` | `cases.py` | `cases.py` | `knowledge/api/routes.py` |
| **Services** | `investigation_session_service.py` | `case_service.py` | `case_service.py` | `knowledge_service.py` |
| **Repositories** | `session_repository.py` | `case_repo.py`, `session_repo.py`, `evidence_repo.py` | `case_repository.py` | `knowledge_repository.py` |
| **Database** | PostgreSQL FTS | PostgreSQL (3 tables) | PostgreSQL aggregation | PostgreSQL + ChromaDB |
| **Caching** | None (real-time) | None (high cardinality) | Redis (1h TTL) | None (async) |
| **Background Jobs** | N/A | N/A | N/A | Celery worker |
| **Auth Required** | JWT User | JWT User | JWT User | JWT Admin |
| **Perf Target** | <500ms | <1s | <2s | 202 <100ms |

---

## API Endpoint Decision Tree

```mermaid
graph TD
    Start[Client Request] --> Auth{Has JWT?}
    Auth -->|No| Unauthorized[401 Unauthorized]
    Auth -->|Yes| ValidateToken{Token Valid?}

    ValidateToken -->|No| Unauthorized
    ValidateToken -->|Yes| Endpoint{Which Endpoint?}

    Endpoint -->|Session Search| CheckSearchParams{Valid Query?}
    Endpoint -->|Case Timeline| CheckTimelineAuth{Own Org?}
    Endpoint -->|Case Trends| CheckTrendsDate{Valid Dates?}
    Endpoint -->|Knowledge Ingest| CheckAdminRole{Is Admin?}

    CheckSearchParams -->|No| ValidationError[422 Validation Error]
    CheckSearchParams -->|Yes| ExecuteSearch[Execute FTS Query]
    ExecuteSearch --> ReturnResults[200 OK + Results]

    CheckTimelineAuth -->|No| Forbidden[403 Forbidden]
    CheckTimelineAuth -->|Yes| CaseExists{Case Exists?}
    CaseExists -->|No| NotFound[404 Not Found]
    CaseExists -->|Yes| GenerateTimeline[Aggregate Events]
    GenerateTimeline --> ReturnTimeline[200 OK + Timeline]

    CheckTrendsDate -->|No| ValidationError
    CheckTrendsDate -->|Yes| CheckCache{Cached?}
    CheckCache -->|Yes| ReturnCached[200 OK + Cached Data]
    CheckCache -->|No| AggregateData[PostgreSQL Aggregation]
    AggregateData --> CacheResult[Store in Redis]
    CacheResult --> ReturnTrends[200 OK + Trends]

    CheckAdminRole -->|No| Forbidden
    CheckAdminRole -->|Yes| ValidateBatch{Valid Batch?}
    ValidateBatch -->|No| ValidationError
    ValidateBatch -->|Yes| QueueJob[Create + Queue Job]
    QueueJob --> ReturnJobID[202 Accepted + Job ID]

    style Start fill:#e1f5ff
    style Unauthorized fill:#ffe1e1
    style Forbidden fill:#ffe1e1
    style NotFound fill:#ffe1e1
    style ValidationError fill:#fff4e1
    style ReturnResults fill:#e1ffe1
    style ReturnTimeline fill:#e1ffe1
    style ReturnTrends fill:#e1ffe1
    style ReturnJobID fill:#e1ffe1
```

---

## Testing Architecture

```mermaid
graph TB
    subgraph "Test Pyramid"
        E2E[End-to-End Tests<br/>4 tests<br/>Full workflow]
        INT[Integration Tests<br/>27 tests<br/>API + DB]
        UNIT[Unit Tests<br/>30 tests<br/>Service logic]
        PERF[Performance Tests<br/>4 tests<br/>Benchmarks]
    end

    subgraph "Test Coverage by Endpoint"
        T1[Session Search<br/>16 tests<br/>8 unit + 8 integration]
        T2[Case Timeline<br/>12 tests<br/>6 unit + 6 integration]
        T3[Case Trends<br/>11 tests<br/>6 unit + 5 integration]
        T4[Knowledge Ingest<br/>18 tests<br/>10 unit + 8 integration]
    end

    subgraph "Test Execution Strategy"
        LOCAL[Local: pytest -k test_name]
        CI[CI: pytest --cov=faultmaven]
        LINT[Linting: import-linter]
    end

    E2E -.->|validates| INT
    INT -.->|validates| UNIT
    PERF -.->|benchmarks| INT

    T1 --> UNIT
    T2 --> UNIT
    T3 --> UNIT
    T4 --> UNIT

    T1 --> INT
    T2 --> INT
    T3 --> INT
    T4 --> INT

    LOCAL --> CI
    CI --> LINT

    style E2E fill:#ffcccc
    style INT fill:#ffffcc
    style UNIT fill:#ccffcc
    style PERF fill:#ccccff
```

---

## Deployment Flow

```mermaid
graph LR
    subgraph "Development"
        DEV[Local Dev]
        TEST[Run Tests]
    end

    subgraph "CI/CD Pipeline"
        BUILD[Build & Lint]
        MIGRATE[Alembic Migrations]
        DEPLOY[Deploy to Staging]
    end

    subgraph "Production"
        CANARY[Canary Deployment<br/>10% traffic]
        FULL[Full Deployment]
        MONITOR[Monitor Metrics]
    end

    DEV --> TEST
    TEST -->|All pass| BUILD
    BUILD --> MIGRATE
    MIGRATE --> DEPLOY
    DEPLOY --> CANARY
    CANARY -->|Success| FULL
    CANARY -->|Failure| ROLLBACK[Rollback]
    FULL --> MONITOR
    MONITOR -->|Issues| ROLLBACK

    style TEST fill:#ccffcc
    style MIGRATE fill:#ffffcc
    style CANARY fill:#ffcccc
    style ROLLBACK fill:#ff9999
```

---

## Monitoring Dashboard Layout

```mermaid
graph TB
    subgraph "Endpoint Metrics"
        M1[Request Rate<br/>req/sec]
        M2[Latency P50/P95/P99<br/>milliseconds]
        M3[Error Rate<br/>4xx/5xx]
    end

    subgraph "Search Metrics"
        S1[Search Query Count]
        S2[Avg Results per Query]
        S3[Zero Results Rate]
    end

    subgraph "Ingest Metrics"
        I1[Jobs Queued/Processing]
        I2[Documents Processed]
        I3[Failure Rate]
        I4[Processing Throughput<br/>docs/sec]
    end

    subgraph "Database Metrics"
        D1[Query Execution Time]
        D2[Index Hit Rate]
        D3[Connection Pool Usage]
    end

    subgraph "Alerts"
        A1[High Latency Alert<br/>> 2s]
        A2[High Error Rate<br/>> 5%]
        A3[Ingest Job Stuck<br/>> 30 min]
    end

    M1 --> A2
    M2 --> A1
    I3 --> A3
    D1 --> A1

    style A1 fill:#ffcccc
    style A2 fill:#ffcccc
    style A3 fill:#ffcccc
```

---

**End of Architecture Diagrams**
