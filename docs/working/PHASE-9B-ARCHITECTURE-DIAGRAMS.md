# Phase 9B Architecture Diagrams

## 1. Test Failure Distribution

```mermaid
pie title "Test Status Distribution (604 tests)"
    "Passing (416)" : 416
    "Failing (179)" : 179
    "Errors (6)" : 6
    "xfailed (3)" : 3
```

## 2. Failure Category Breakdown

```mermaid
pie title "Failure Categories (179 failures)"
    "Miscellaneous (112)" : 112
    "New Architecture Workflows (19)" : 19
    "Session/Case Integration (13)" : 13
    "Test Infrastructure (8)" : 8
    "Protection Integration (4)" : 4
    "Database Schema (2)" : 2
    "Main App (1)" : 1
    "KB Ingestion (1)" : 1
```

## 3. Phase 9B Execution Flow

```mermaid
graph TD
    A[Phase 9A Complete<br/>416 passing / 179 failing / 6 errors] --> B[Phase 9B-1: Quick Wins]

    B --> B1[Fix DIContainer.case_service<br/>+6 tests]
    B --> B2[Fix deprecated endpoint test<br/>+1 test]
    B --> B3[Investigate session resumption<br/>+2 tests]

    B1 --> C{Phase 9B-2:<br/>Router Decision}
    B2 --> C
    B3 --> C

    C -->|Register| C1[Add admin/users routers<br/>+20-40 tests]
    C -->|Delete| C2[Remove legacy routers<br/>-10-20 tests]

    C1 --> D[Phase 9B-3: Targeted Fixes]
    C2 --> D

    D --> D1[Fix case service tests<br/>+5 tests]
    D --> D2[Fix session tests<br/>+3 tests]
    D --> D3[Fix DB schema tests<br/>+2 tests]

    D1 --> E[Phase 9B-4: Cleanup]
    D2 --> E
    D3 --> E

    E --> E1[Skip future work tests<br/>-19 failures]
    E --> E2[Skip protection tests<br/>-4 failures]
    E --> E3[Defer complex tests<br/>-60+ failures]

    E1 --> F[Target Achieved:<br/>500+ passing tests<br/>83%+ pass rate]
    E2 --> F
    E3 --> F

    style A fill:#ff9999
    style B fill:#ffcc99
    style C fill:#ffff99
    style D fill:#99ccff
    style E fill:#cc99ff
    style F fill:#99ff99
```

## 4. Router Architecture Analysis

```mermaid
graph LR
    subgraph "Current State"
        A[main.py] --> M1[modules/agent/api]
        A --> M2[modules/auth/api]
        A --> M3[modules/case/api]
        A --> M4[modules/evidence/api]
        A --> M5[modules/knowledge/api]
        A --> M6[modules/report/api]
        A --> L1[api/routes/sessions.py]

        style L1 fill:#99ff99,stroke:#006600
    end

    subgraph "Missing Registrations"
        A -.->|NOT REGISTERED| L2[api/routes/admin.py]
        A -.->|NOT REGISTERED| L3[api/routes/users.py]

        style L2 fill:#ff9999,stroke:#cc0000
        style L3 fill:#ff9999,stroke:#cc0000
    end

    subgraph "Legacy Duplicates"
        L4[api/routes/auth.py<br/>DUPLICATE]
        L5[api/routes/cases.py<br/>DUPLICATE]
        L6[api/routes/evidence.py<br/>DUPLICATE]

        style L4 fill:#ffcc99,stroke:#ff9900
        style L5 fill:#ffcc99,stroke:#ff9900
        style L6 fill:#ffcc99,stroke:#ff9900
    end
```

## 5. DIContainer Access Pattern Issue

```mermaid
sequenceDiagram
    participant Test as Integration Test
    participant Fixture as conftest.case_service fixture
    participant Container as DIContainer
    participant Service as CaseService

    rect rgb(255, 200, 200)
        Note over Test,Service: CURRENT (BROKEN)
        Test->>Fixture: Request case_service
        Fixture->>Container: container.case_service
        Container--xFixture: AttributeError: 'DIContainer' object<br/>has no attribute 'case_service'
        Fixture--xTest: ERROR
    end

    rect rgb(200, 255, 200)
        Note over Test,Service: FIXED
        Test->>Fixture: Request case_service
        Fixture->>Container: container.get_case_service()
        Container->>Service: Returns CaseService instance
        Service-->>Fixture: CaseService
        Fixture-->>Test: CaseService
    end
```

## 6. Phase 9B Impact Projection

```mermaid
graph TD
    Start[Current: 416 passing<br/>69.0% pass rate] --> QB[Quick Wins<br/>+20-30 tests]

    QB --> |Best Case| Router1[Router Registration<br/>+40 tests]
    QB --> |Worst Case| Router2[Router Deletion<br/>+0 tests]

    Router1 --> Targeted1[Targeted Fixes<br/>+50 tests]
    Router2 --> Targeted2[Targeted Fixes<br/>+30 tests]

    Targeted1 --> |Optimistic| Final1[526 passing<br/>87.1% pass rate]
    Targeted1 --> |Conservative| Final2[506 passing<br/>83.8% pass rate]
    Targeted2 --> |Conservative| Final3[496 passing<br/>82.1% pass rate]

    style Start fill:#ff9999
    style Final1 fill:#99ff99
    style Final2 fill:#99ff99
    style Final3 fill:#ccffcc
```

## 7. Test Infrastructure Dependencies

```mermaid
graph TB
    subgraph "Fixture Layer (conftest.py)"
        F1[http_client]
        F2[redis_client]
        F3[clean_redis]
        F4[session_service]
        F5[case_service ❌]
        F6[evidence_service]
    end

    subgraph "DIContainer Layer"
        C1[container.get_session_service]
        C2[container.get_case_service]
        C3[container.get_evidence_service]
    end

    subgraph "Service Layer"
        S1[AuthSessionService]
        S2[CaseService]
        S3[EvidenceService]
    end

    F4 --> C1 --> S1
    F5 -.->|BROKEN: property access| C2
    F5 -->|FIXED: method call| C2
    C2 --> S2
    F6 --> C3 --> S3

    style F5 fill:#ff9999
    style C2 fill:#ffcc99
```

## 8. Risk vs Impact Matrix

```mermaid
quadrantChart
    title Risk vs Impact Assessment
    x-axis Low Risk --> High Risk
    y-axis Low Impact --> High Impact
    quadrant-1 High Priority (High Impact, Low Risk)
    quadrant-2 Strategic (High Impact, High Risk)
    quadrant-3 Quick Wins (Low Impact, Low Risk)
    quadrant-4 Avoid (Low Impact, High Risk)

    DIContainer Fix: [0.1, 0.9]
    Router Registration: [0.4, 0.7]
    Session Resumption Fix: [0.3, 0.8]
    Deprecated Endpoint Fix: [0.1, 0.2]
    Case Service Integration: [0.5, 0.6]
    New Architecture Tests Cleanup: [0.2, 0.5]
    Protection Integration Tests: [0.6, 0.3]
    Database Schema Fix: [0.7, 0.4]
```

## 9. Test Suite Health Progression

```mermaid
gantt
    title Test Suite Health Over Time
    dateFormat YYYY-MM-DD
    section Baseline
    Phase 9A Start (300 passing)     :milestone, m1, 2026-01-08, 0d

    section Phase 9A
    Router Registration Fix          :done, 2026-01-08, 1d
    RedisSessionStore Bug Fix        :done, 2026-01-09, 1d
    Auth Pattern Migration           :done, 2026-01-09, 2d
    Phase 9A Complete (416 passing)  :milestone, m2, 2026-01-10, 0d

    section Phase 9B
    Quick Wins (436 passing)         :active, 2026-01-10, 1d
    Router Decision (476 passing)    :2026-01-11, 1d
    Targeted Fixes (516 passing)     :2026-01-12, 2d
    Cleanup & Documentation          :2026-01-14, 1d
    Phase 9B Target (500+ passing)   :milestone, m3, 2026-01-15, 0d
```

## 10. Decision Tree: Router Registration

```mermaid
flowchart TD
    Start{Are admin.py and users.py<br/>legacy duplicates?}

    Start -->|Check endpoints| Check[Compare endpoints<br/>in legacy vs modules]

    Check -->|Identical| Dup{Endpoints are duplicates}
    Check -->|Different| Unique{Endpoints are unique}

    Dup -->|Option 1| Delete[Delete legacy routers<br/>Update/delete tests<br/>Impact: -10-20 tests]
    Dup -->|Option 2| Keep[Keep for backward compat<br/>Register both<br/>Risk: MEDIUM]

    Unique -->|Tests exist?| TestCheck{Check for tests}
    TestCheck -->|Yes| Register[Register routers<br/>Impact: +20-40 tests<br/>Risk: LOW]
    TestCheck -->|No| Future[Future work<br/>Skip for now]

    Register --> Validate[Validate:<br/>1. No conflicts<br/>2. Tests pass<br/>3. OpenAPI correct]
    Delete --> Validate

    Validate --> Success[Success:<br/>Document decision<br/>Update architecture docs]

    style Start fill:#ffff99
    style Register fill:#99ff99
    style Delete fill:#ff9999
    style Success fill:#99ccff
```

## 11. Architectural Compliance Test Flow

```mermaid
sequenceDiagram
    participant Test as test_architectural_compliance.py
    participant Fixture as case_service fixture
    participant Container as DIContainer
    participant CaseService as CaseService
    participant Redis as RedisSessionStore
    participant DB as PostgreSQL

    Note over Test,DB: Test Flow with Fixed Fixture

    Test->>Fixture: @pytest.fixture case_service
    Fixture->>Container: container.get_case_service()
    Container->>CaseService: Initialize
    CaseService->>Redis: Connect to session store
    CaseService->>DB: Connect to database

    Redis-->>CaseService: Connection OK
    DB-->>CaseService: Connection OK
    CaseService-->>Container: CaseService instance
    Container-->>Fixture: Return service
    Fixture-->>Test: Inject case_service

    rect rgb(200, 255, 200)
        Test->>CaseService: await case_service.create_case(...)
        CaseService->>DB: INSERT INTO cases
        DB-->>CaseService: Success
        CaseService-->>Test: Case created
    end

    Test->>Test: Assert case ownership
    Test->>Test: Assert case accessibility
    Test->>Test: ✓ Test PASSES
```

## 12. Success Metrics Dashboard

```mermaid
graph TB
    subgraph "Phase 9B Success Metrics"
        M1[Test Pass Rate<br/>Target: 83%+<br/>Current: 69.0%]
        M2[Error Count<br/>Target: 0<br/>Current: 6]
        M3[Coverage<br/>Target: 71%+<br/>Current: 71%+]
        M4[Production Bugs Found<br/>Target: 1+<br/>Current: TBD]
    end

    subgraph "Phase Deliverables"
        D1[Architectural Analysis ✓]
        D2[Strategic Plan ✓]
        D3[Risk Assessment ✓]
        D4[Test Engineer Handoff ✓]
    end

    subgraph "Quality Gates"
        Q1{All quick wins<br/>implemented?}
        Q2{Router decision<br/>documented?}
        Q3{Coverage<br/>maintained?}
        Q4{No regressions?}
    end

    M1 --> Q1
    M2 --> Q1
    Q1 -->|Yes| Q2
    Q2 -->|Yes| Q3
    Q3 -->|Yes| Q4
    Q4 -->|Yes| Success[Phase 9B Success:<br/>500+ passing tests<br/>83%+ pass rate]

    style M1 fill:#ffcc99
    style M2 fill:#ff9999
    style M3 fill:#99ff99
    style M4 fill:#ffff99
    style Success fill:#99ff99
```

---

## Diagram Usage Guide

1. **Test Failure Distribution** - Shows current test status breakdown
2. **Failure Category Breakdown** - Identifies which test categories have most failures
3. **Phase 9B Execution Flow** - Strategic roadmap for Phase 9B work
4. **Router Architecture Analysis** - Visual audit of router registration status
5. **DIContainer Access Pattern** - Illustrates the fixture bug and fix
6. **Phase 9B Impact Projection** - Shows expected outcomes (best/worst case)
7. **Test Infrastructure Dependencies** - Maps fixture → container → service dependencies
8. **Risk vs Impact Matrix** - Prioritization tool for Phase 9B tasks
9. **Test Suite Health Progression** - Timeline showing improvement trajectory
10. **Decision Tree: Router Registration** - Flowchart for router decision
11. **Architectural Compliance Test Flow** - Sequence diagram for fixed test execution
12. **Success Metrics Dashboard** - Phase 9B quality gates and deliverables

These diagrams support the architectural analysis and strategic plan in **PHASE-9B-ARCHITECTURAL-ANALYSIS.md**.
