# FaultMaven Enterprise Microservices Migration Plan

**Version**: 3.0 (Production-Hardened)
**Date**: 2025-11-15
**Status**: Enterprise-Ready
**Supersedes**: MIGRATION_EXECUTION_PLAN.md (public/private split only)
**Enhancements**:
- v2.0: Service extraction decision criteria, ports/adapters strangler pattern, containerization best practices
- v3.0: Outbox/inbox pattern for exactly-once events, event schema versioning, API lifecycle & deprecation policy (N+2 rule), breaking change detection

---

## Executive Summary

This document provides a comprehensive execution plan for transforming FaultMaven from a monolithic Python application into an enterprise-ready, microservices-based SaaS platform running on Kubernetes.

**Two Deployment Models**:

1. **Public Open-Source (Monolithic)**
   - Single deployable application
   - Local deployment (`pip install faultmaven` → `faultmaven serve`)
   - Accessible at `http://localhost:8000` (configurable port)
   - Core investigation features (Chat API + Knowledge Base API)
   - Simplified storage (SQLite/in-memory, no PostgreSQL/Redis/ChromaDB)
   - No enterprise multi-tenancy
   - Apache 2.0 license

2. **Private Enterprise (Microservices)**
   - 8 independently deployable services
   - Kubernetes-based SaaS platform at `https://app.faultmaven.ai`
   - Multi-tenant architecture (organizations, teams, RBAC)
   - Horizontal scalability with service-specific databases
   - Advanced security and compliance
   - PostgreSQL per service + Redis cluster + ChromaDB cluster
   - Proprietary license

**Key Objectives**:
- Decompose monolith into bounded contexts with clear data ownership
- Implement strangler pattern for zero-downtime migration
- Establish API-first contracts with versioning
- Enable independent service deployment and scaling
- Achieve operational excellence with SLOs and observability

**Timeline**: 12 weeks
**Effort**: 3-4 full-time engineers
**Risk Level**: Medium (mitigated by strangler pattern)

---

## Table of Contents

1. [Current State Analysis](#1-current-state-analysis)
2. [Enterprise Architecture Principles](#2-enterprise-architecture-principles)
3. [Bounded Contexts & Service Decomposition](#3-bounded-contexts--service-decomposition)
4. [Data Ownership Matrix](#4-data-ownership-matrix)
5. [Service Contract Specifications](#5-service-contract-specifications)
6. [Strangler Pattern Migration Strategy](#6-strangler-pattern-migration-strategy)
7. [Containerization Best Practices](#7-containerization-best-practices)
8. [Phase-by-Phase Execution Plan](#8-phase-by-phase-execution-plan)
9. [Kubernetes Deployment Architecture](#9-kubernetes-deployment-architecture)
10. [Operational Readiness](#10-operational-readiness)
11. [Success Metrics](#11-success-metrics)

---

## 1. Current State Analysis

### 1.1 Monolithic Architecture Overview

**Repository**: `https://github.com/sterlanyu/FaultMaven`

**Current Architecture** (v3.0):
- **Lines of Code**: ~35,000+ Python (247 files)
- **Storage**: PostgreSQL (10 tables) + Redis (sessions) + ChromaDB (vectors)
- **Investigation Engine**: Milestone-based with MilestoneEngine
- **Services**: Case, Investigation, Data, Knowledge, Session
- **Test Coverage**: 1425+ tests (71% coverage)

**Database Schema Analysis**:

PostgreSQL Tables (10 total):
```
Core Case Data:
  cases                       # User-owned investigation cases
  case_messages              # Conversation history
  case_status_transitions    # Audit trail
  case_tags                  # User categorization

Evidence & Analysis:
  evidence                   # Structured evidence artifacts
  uploaded_files            # File metadata and processing
  hypotheses                # Root cause theories
  solutions                 # Proposed fixes
  agent_tool_calls          # Tool execution audit

Enterprise Features:
  users                     # User accounts
  organizations             # Multi-tenant workspaces
  organization_members      # User-org mapping
  teams                     # Sub-org collaboration
  team_members              # User-team mapping
  roles                     # RBAC role definitions
  permissions               # RBAC permissions
  role_permissions          # Role-permission mapping
  user_audit_log           # Security audit trail
  kb_documents             # Knowledge base metadata
  kb_document_shares       # KB sharing permissions
```

**Service Layer Analysis**:

Current Services (Monolithic):
```python
# Domain Services
- CaseService             # Case lifecycle management
- SessionService          # Redis-backed sessions
- DataService             # File upload & processing
- KnowledgeService        # KB document management
- PlanningService         # Investigation planning

# Analytics Services
- AnalyticsDashboardService
- ConfidenceService

# Agentic Framework
- AgentStateManager
- ToolSkillBroker
- GuardrailsPolicyLayer
- ResponseSynthesizer
- ErrorFallbackManager
- BusinessLogicWorkflowEngine
```

**Dependencies**:
- FastAPI (API layer)
- SQLAlchemy (ORM)
- Redis (sessions/cache)
- ChromaDB (vector store)
- Presidio (PII redaction)
- OpenAI/Anthropic/Fireworks/Groq (LLMs)
- Opik (tracing)

### 1.2 Current Pain Points

**Scalability Limitations**:
- Monolithic deployment prevents independent scaling
- Single PostgreSQL database becomes bottleneck
- Cannot scale investigation engine separately from KB search
- Session service tied to application lifecycle

**Operational Challenges**:
- Full application restart for any code change
- No isolation between features (auth failure affects investigation)
- Difficult to implement feature flags per service
- Hard to track resource usage per feature

**Development Velocity**:
- Large codebase requires all developers to understand everything
- Tight coupling makes parallel development difficult
- Testing requires entire application stack
- Long CI/CD pipelines

**Enterprise Requirements Not Met**:
- No multi-tenant data isolation at infrastructure level
- Cannot deploy customer-specific customizations
- Difficult to implement tiered service plans
- No granular SLOs per feature

---

## 2. Enterprise Architecture Principles

### 2.1 Core Principles

#### Single-Writer Data Ownership
**Definition**: Each service exclusively owns and writes to its own database schema.

**Rules**:
- ✅ **DO**: Service writes to its own database
- ✅ **DO**: Service exposes API for others to read its data
- ❌ **DON'T**: Service queries another service's database directly
- ❌ **DON'T**: Shared database between services

**Example**:
```
Case Service owns `cases` table:
  ✅ Case Service writes to cases table
  ✅ Investigation Service calls GET /cases/{id} API
  ❌ Investigation Service queries cases table directly
```

**Benefits**:
- Clear ownership and accountability
- Independent schema evolution
- Service can be replaced without impacting others
- Database technology can differ per service (PostgreSQL, MongoDB, etc.)

#### API-First Design
**Definition**: Design and document API contracts before implementation.

**Process**:
1. Write OpenAPI 3.1 specification
2. Review with consuming teams
3. Generate server stubs and client SDKs
4. Implement against contract
5. Automated contract testing in CI/CD

**Versioning Strategy**:
```
/v1/cases      # Stable, must maintain backwards compatibility
/v2/cases      # Breaking changes, new major version
/v1beta/cases  # Experimental, no stability guarantees
```

**Example Contract**:
```yaml
# fm-case-service/openapi/case-api.yaml
openapi: 3.1.0
info:
  title: Case Service API
  version: 1.0.0
paths:
  /v1/cases:
    post:
      operationId: createCase
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CaseCreateRequest'
```

#### Event-Driven Async Workflows
**Definition**: Use events for cross-service communication when immediate response not needed.

**Patterns**:

1. **Choreography** (Loosely Coupled):
```
User deleted from Auth Service
  → Publishes: auth.user.deleted event
  → Case Service subscribes → deletes user's cases
  → KB Service subscribes → deletes user's documents
```

2. **Orchestration** (Centralized Control):
```
Agent Orchestrator coordinates investigation:
  → Calls Case Service: Get case details
  → Calls Knowledge Service: Search relevant docs
  → Calls Investigation Service: Generate hypotheses
  → Returns coordinated result
```

**Event Schema** (AsyncAPI):
```yaml
# events/case-events.yaml
asyncapi: 2.6.0
channels:
  case.created:
    publish:
      message:
        payload:
          type: object
          properties:
            case_id: string
            user_id: string
            created_at: string
```

#### Failure Isolation
**Definition**: Service failures must not cascade to other services.

**Patterns**:
- **Circuit Breaker**: Stop calling failing service after N failures
- **Bulkhead**: Separate thread pools per service call
- **Timeout**: All service calls have maximum timeout
- **Fallback**: Serve degraded response if dependency unavailable

**Example**:
```python
# Circuit breaker for KB Service
@circuit_breaker(failure_threshold=5, timeout=30)
async def search_knowledge_base(query: str):
    try:
        return await kb_client.search(query, timeout=5)
    except TimeoutError:
        # Fallback: Return empty results instead of error
        logger.warning("KB service timeout, returning empty results")
        return {"results": [], "degraded": True}
```

#### SLO-Driven Scaling
**Definition**: Scale services based on Service Level Objectives, not arbitrary metrics.

**SLO Hierarchy**:
```
Service SLO → Application SLO → Business SLO

Example:
  Business SLO: 99.9% uptime for paid customers
  Application SLO: Investigation workflow 99.5% success rate
  Service SLOs:
    - Auth Service: 99.9% availability (most critical)
    - Case Service: 99.5% availability
    - KB Service: 99.0% availability (can degrade)
```

**Auto-Scaling Rules**:
```yaml
# HorizontalPodAutoscaler for Case Service
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: fm-case-service
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: fm-case-service
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Pods
    pods:
      metric:
        name: http_request_duration_p99
      target:
        type: AverageValue
        averageValue: "500m"  # 500ms p99 latency
```

---

## 3. Bounded Contexts & Service Decomposition

### 3.1 Service Topology

Based on Domain-Driven Design analysis of the FaultMaven codebase:

```
┌─────────────────────────────────────────────────────────────┐
│                      API Gateway (Kong/NGINX)               │
│  - Authentication Middleware                                 │
│  - Rate Limiting                                            │
│  - Request Routing                                          │
│  - Distributed Tracing (Correlation IDs)                   │
└────────────┬────────────────────────────────────────────────┘
             │
    ┌────────┴────────┬────────────┬──────────┬──────────┐
    │                 │            │          │          │
    v                 v            v          v          v
┌────────┐      ┌──────────┐  ┌──────┐  ┌────────┐  ┌──────────┐
│  Auth  │      │   Case   │  │ Evid │  │ Invest │  │ Session  │
│Service │      │ Service  │  │Service│  │Service │  │ Service  │
└────────┘      └──────────┘  └──────┘  └────────┘  └──────────┘
    │                 │          │          │          │
    v                 v          v          v          v
┌────────┐      ┌──────────┐  ┌──────┐  ┌────────┐  ┌──────────┐
│  Auth  │      │   Case   │  │ Evid │  │ Invest │  │  Redis   │
│   DB   │      │    DB    │  │  DB  │  │   DB   │  │ Cluster  │
└────────┘      └──────────┘  └──────┘  └────────┘  └──────────┘

    ┌──────────┬──────────┬──────────────┐
    │          │          │              │
    v          v          v              v
┌──────────┐┌──────────┐┌───────────┐┌──────────┐
│Knowledge ││  Agent   ││Analytics  ││ Upload   │
│ Service  ││Orchestr. ││ Service   ││ Service  │
└──────────┘└──────────┘└───────────┘└──────────┘
    │            │            │            │
    v            │            v            v
┌──────────┐    │       ┌───────────┐┌──────────┐
│ ChromaDB │    │       │AnalyticsDB││  S3/GCS  │
│ Cluster  │    └──────▶│ (TimeSeries)││ Buckets │
└──────────┘            └───────────┘└──────────┘
```

### 3.2 Service Definitions

#### 3.2.1 Auth Service (`fm-auth-service`)

**Bounded Context**: User Identity & Access Management

**Responsibilities**:
- User authentication (login, logout, token management)
- User registration and profile management
- Organization/team management (multi-tenancy)
- RBAC (roles and permissions)
- SSO integration (future)
- Security audit logging

**Data Ownership**:
- PostgreSQL Tables:
  - `users`
  - `organizations`
  - `organization_members`
  - `teams`
  - `team_members`
  - `roles`
  - `permissions`
  - `role_permissions`
  - `user_audit_log`

**API Endpoints**:
```
POST   /v1/auth/login
POST   /v1/auth/logout
POST   /v1/auth/register
GET    /v1/auth/me
PUT    /v1/auth/me
GET    /v1/users/{user_id}
POST   /v1/organizations
GET    /v1/organizations/{org_id}
POST   /v1/organizations/{org_id}/members
DELETE /v1/organizations/{org_id}/members/{user_id}
```

**Events Published**:
```
auth.user.created
auth.user.updated
auth.user.deleted
auth.organization.created
auth.team.created
auth.role.assigned
```

**Technology Stack**:
- FastAPI
- PostgreSQL (dedicated auth database)
- Redis (token blacklist)
- JWT tokens (RS256 asymmetric)

**Scaling Characteristics**:
- Read-heavy (token validation on every request)
- Horizontal scaling with read replicas
- Redis cache for frequently accessed user data
- SLO: 99.9% availability, <100ms p99 latency

---

#### 3.2.2 Case Service (`fm-case-service`)

**Bounded Context**: Case Lifecycle Management

**Responsibilities**:
- Case CRUD operations
- Case status management (CONSULTING → INVESTIGATING → RESOLVED → CLOSED)
- Case-session association
- Case sharing and permissions
- Case search and filtering
- Case message history

**Data Ownership**:
- PostgreSQL Tables:
  - `cases`
  - `case_messages`
  - `case_status_transitions`
  - `case_tags`

**API Endpoints**:
```
POST   /v1/cases
GET    /v1/cases/{case_id}
PUT    /v1/cases/{case_id}
DELETE /v1/cases/{case_id}
GET    /v1/cases
POST   /v1/cases/{case_id}/messages
GET    /v1/cases/{case_id}/messages
POST   /v1/cases/{case_id}/tags
GET    /v1/users/{user_id}/cases
```

**Events Published**:
```
case.created
case.updated
case.status_changed
case.deleted
case.message.added
```

**Events Subscribed**:
```
auth.user.deleted → cascade delete user's cases
```

**Technology Stack**:
- FastAPI
- PostgreSQL (dedicated case database)
- Full-text search (PostgreSQL tsvector)

**Scaling Characteristics**:
- Read-heavy (dashboard views)
- Write-moderate (user interactions)
- Database read replicas for queries
- Caching for frequently accessed cases
- SLO: 99.5% availability, <500ms p99 latency

---

#### 3.2.3 Evidence Service (`fm-evidence-service`)

**Bounded Context**: Evidence & File Management

**Responsibilities**:
- Evidence artifact management
- File upload coordination
- File processing status tracking
- Content categorization (LOGS, CONFIG, METRICS, etc.)
- Evidence search within case

**Data Ownership**:
- PostgreSQL Tables:
  - `evidence`
  - `uploaded_files`
  - `agent_tool_calls`

**API Endpoints**:
```
POST   /v1/evidence
GET    /v1/evidence/{evidence_id}
DELETE /v1/evidence/{evidence_id}
GET    /v1/cases/{case_id}/evidence
POST   /v1/files/upload
GET    /v1/files/{file_id}
GET    /v1/files/{file_id}/status
```

**Events Published**:
```
evidence.created
evidence.deleted
file.uploaded
file.processing.completed
file.processing.failed
```

**Events Subscribed**:
```
case.deleted → cascade delete case evidence
```

**Technology Stack**:
- FastAPI
- PostgreSQL (evidence metadata)
- S3/GCS (file storage)
- Background workers (Celery) for file processing

**Scaling Characteristics**:
- Write-heavy (file uploads)
- Object storage for scalability
- Async processing with job queues
- SLO: 99.0% availability, <2s p99 latency

---

#### 3.2.4 Investigation Service (`fm-investigation-service`)

**Bounded Context**: Hypothesis & Solution Management

**Responsibilities**:
- Hypothesis lifecycle management
- Solution proposal and tracking
- Hypothesis validation
- Confidence scoring
- Investigation analytics

**Data Ownership**:
- PostgreSQL Tables:
  - `hypotheses`
  - `solutions`

**API Endpoints**:
```
POST   /v1/hypotheses
GET    /v1/hypotheses/{hypothesis_id}
PUT    /v1/hypotheses/{hypothesis_id}
DELETE /v1/hypotheses/{hypothesis_id}
GET    /v1/cases/{case_id}/hypotheses
POST   /v1/solutions
GET    /v1/solutions/{solution_id}
PUT    /v1/solutions/{solution_id}
GET    /v1/cases/{case_id}/solutions
```

**Events Published**:
```
hypothesis.proposed
hypothesis.validated
hypothesis.invalidated
solution.proposed
solution.implemented
```

**Events Subscribed**:
```
case.deleted → cascade delete hypotheses and solutions
evidence.created → trigger hypothesis generation
```

**Technology Stack**:
- FastAPI
- PostgreSQL (investigation data)

**Scaling Characteristics**:
- Read-moderate, write-moderate
- Stateless service (easy horizontal scaling)
- SLO: 99.0% availability, <1s p99 latency

---

#### 3.2.5 Session Service (`fm-session-service`)

**Bounded Context**: Session Management

**Responsibilities**:
- Session creation and lifecycle
- Session TTL management
- Rate limiting per session
- Session-case association tracking
- Client resumption support

**Data Ownership**:
- Redis (ephemeral session storage, no PostgreSQL)

**API Endpoints**:
```
POST   /v1/sessions
GET    /v1/sessions/{session_id}
DELETE /v1/sessions/{session_id}
PUT    /v1/sessions/{session_id}/heartbeat
GET    /v1/users/{user_id}/sessions
```

**Events Published**:
```
session.created
session.expired
session.terminated
```

**Technology Stack**:
- FastAPI
- Redis Cluster (primary storage)
- No PostgreSQL dependency

**Scaling Characteristics**:
- Extremely high throughput (every request checks session)
- Redis cluster with replication
- Horizontal scaling (stateless)
- SLO: 99.9% availability, <50ms p99 latency

---

#### 3.2.6 Knowledge Service (`fm-knowledge-service`)

**Bounded Context**: Knowledge Base & Vector Search

**Responsibilities**:
- Document ingestion (User KB, Global KB, Case Evidence)
- Vector embedding generation
- Semantic search / RAG retrieval
- Knowledge base management
- Document sharing permissions

**Data Ownership**:
- ChromaDB (3 separate collections):
  - `user_kb_{user_id}` - User personal knowledge
  - `global_kb` - System-wide knowledge
  - `case_evidence_{case_id}` - Case-specific evidence
- PostgreSQL (metadata only):
  - `kb_documents`
  - `kb_document_shares`

**API Endpoints**:
```
POST   /v1/kb/documents
GET    /v1/kb/documents/{doc_id}
DELETE /v1/kb/documents/{doc_id}
POST   /v1/kb/search
POST   /v1/kb/user/{user_id}/documents
POST   /v1/kb/global/documents
POST   /v1/kb/case/{case_id}/documents
```

**Events Published**:
```
kb.document.ingested
kb.document.deleted
kb.search.completed
```

**Events Subscribed**:
```
auth.user.deleted → delete user KB
case.deleted → delete case evidence KB
evidence.created → ingest into case KB
```

**Technology Stack**:
- FastAPI
- ChromaDB (vector storage)
- PostgreSQL (metadata)
- Embedding models (BGE-M3, OpenAI embeddings)

**Scaling Characteristics**:
- Read-heavy (search queries)
- ChromaDB cluster with replication
- Async ingestion pipeline
- SLO: 99.0% availability, <2s p99 latency

---

#### 3.2.7 Agent Orchestrator Service (`fm-agent-service`)

**Bounded Context**: AI Investigation Orchestration

**Responsibilities**:
- Milestone-based investigation coordination
- LLM provider routing (OpenAI, Anthropic, Fireworks, Groq)
- Agent tool execution (KB search, web search)
- Investigation workflow state machine
- Response synthesis

**Data Ownership**:
- None (stateless orchestrator)
- Calls other services for data

**API Endpoints**:
```
POST   /v1/investigate
POST   /v1/investigate/turn
GET    /v1/investigate/status/{case_id}
POST   /v1/tools/execute
```

**Dependencies** (API calls to):
- Case Service - Get case details
- Evidence Service - Get evidence
- Investigation Service - Manage hypotheses/solutions
- Knowledge Service - RAG search
- External LLM APIs

**Technology Stack**:
- FastAPI
- No database (stateless)
- In-memory caching (per-request only)
- LLM client libraries

**Scaling Characteristics**:
- CPU-intensive (LLM calls)
- Horizontal scaling (stateless)
- Rate limiting per user/org
- Circuit breakers for LLM APIs
- SLO: 95% availability, <10s p99 latency (LLM calls slow)

---

#### 3.2.8 Analytics Service (`fm-analytics-service`)

**Bounded Context**: Usage Analytics & Reporting

**Responsibilities**:
- Usage metrics collection
- Dashboard data aggregation
- Billing metering
- Performance analytics

**Data Ownership**:
- TimescaleDB (time-series metrics)

**Technology Stack**:
- FastAPI
- TimescaleDB or ClickHouse

**Scaling Characteristics**:
- Write-heavy (event ingestion)
- Read-moderate (dashboard queries)
- SLO: 95% availability, <3s p99 latency

---

#### 3.2.9 Upload Service (`fm-upload-service`)

**Bounded Context**: File Upload & Storage

**Responsibilities**:
- Presigned URL generation
- Direct S3/GCS uploads
- Antivirus scanning integration
- File lifecycle management

**Data Ownership**:
- Object storage (S3/GCS)

**Technology Stack**:
- FastAPI
- S3/GCS SDKs
- ClamAV for scanning

**Scaling Characteristics**:
- Stateless
- Horizontal scaling
- SLO: 99% availability, <1s p99 latency

---

### 3.3 Service Extraction Decision Criteria

**When to Extract a Service** (decision tree):

Use these criteria to decide if a module should become an independent service:

| Criterion | Question | Example |
|-----------|----------|---------|
| **Distinct Storage Technology** | Does it require different storage (PostgreSQL vs Redis vs ChromaDB)? | ✅ Session Service (Redis) separate from Case Service (PostgreSQL) |
| **Independent Scaling Needs** | Does it have different scaling characteristics? | ✅ Knowledge Service (CPU-heavy vector search) vs Auth Service (read-heavy) |
| **Clear Domain Boundary** | Does it have a well-defined domain glossary and bounded context? | ✅ Case (title, status, evidence) vs Auth (user, org, role) |
| **Failure Isolation Benefit** | Would isolating failures improve system resilience? | ✅ KB Service down shouldn't block case creation |
| **Single-Team Ownership** | Can one team own the entire service lifecycle? | ✅ Identity team owns Auth Service end-to-end |
| **SLO Requirements Differ** | Does it need different uptime/latency guarantees? | ✅ Auth Service 99.9% vs Analytics 95% |

**Decision Rule**:
- If **3+ criteria are YES**: Extract as separate service
- If **1-2 criteria are YES**: Consider extracting later (Phase 2)
- If **0 criteria are YES**: Keep in monolith

**Application to FaultMaven Services**:

```
Auth Service:
  ✅ Distinct storage: PostgreSQL (user tables)
  ✅ Independent scaling: High read volume (every request)
  ✅ Clear domain: Identity & access management
  ✅ Failure isolation: Auth failure shouldn't block case operations
  ✅ Single-team: Identity/Security team
  ✅ SLO: 99.9% uptime (higher than average)
  → Score: 6/6 → Extract in Phase 1

Session Service:
  ✅ Distinct storage: Redis (not PostgreSQL)
  ✅ Independent scaling: Extremely high throughput
  ✅ Clear domain: Session lifecycle
  ✅ Failure isolation: Session failure shouldn't block auth
  ❌ Single-team: Overlaps with Auth team
  ✅ SLO: 99.9% uptime, <50ms latency
  → Score: 5/6 → Extract in Phase 1

Evidence Service:
  ✅ Distinct storage: S3 + PostgreSQL
  ✅ Independent scaling: Write-heavy (file uploads)
  ✅ Clear domain: Evidence artifacts
  ✅ Failure isolation: File upload failure shouldn't block case creation
  ❌ Single-team: Overlaps with Case team
  ❌ SLO: Same as Case Service (99.5%)
  → Score: 3/6 → Extract in Phase 2 (defer initially)

Analytics Service:
  ✅ Distinct storage: TimescaleDB (time-series)
  ✅ Independent scaling: Write-heavy event ingestion
  ✅ Clear domain: Usage metrics
  ✅ Failure isolation: Analytics down shouldn't affect core features
  ✅ Single-team: Data/Analytics team
  ❌ SLO: Lower SLO (95% acceptable)
  → Score: 5/6 → Extract in Phase 2 (not critical path)
```

---

### 3.4 Minimal Viable Service Set (MVP)

For initial deployment, start with these 5 core services:

1. **Auth Service** - Authentication and authorization (critical path, 6/6 score)
2. **Session Service** - Session management (critical path, 5/6 score)
3. **Case Service** - Core case management (critical path, 5/6 score)
4. **Agent Orchestrator** - Investigation workflows (core value, stateless)
5. **Knowledge Service** - Vector search (core value, distinct storage)

**Defer to Phase 2**:
- Evidence Service (can be part of Case Service initially, 3/6 score)
- Investigation Service (can be part of Agent Orchestrator initially)
- Analytics Service (not critical path, 5/6 but lower priority)
- Upload Service (use direct monolith upload initially)

---

## 4. Data Ownership Matrix

### 4.1 Database Ownership

| Service | Database Type | Tables Owned | Access Pattern |
|---------|--------------|--------------|----------------|
| Auth Service | PostgreSQL | users, organizations, organization_members, teams, team_members, roles, permissions, role_permissions, user_audit_log | Write: Auth Service only<br>Read: Via Auth API |
| Case Service | PostgreSQL | cases, case_messages, case_status_transitions, case_tags | Write: Case Service only<br>Read: Via Case API |
| Evidence Service | PostgreSQL + S3 | evidence, uploaded_files, agent_tool_calls | Write: Evidence Service only<br>Read: Via Evidence API |
| Investigation Service | PostgreSQL | hypotheses, solutions | Write: Investigation Service only<br>Read: Via Investigation API |
| Session Service | Redis | session:{session_id} keys | Write: Session Service only<br>Read: Via Session API |
| Knowledge Service | ChromaDB + PostgreSQL | ChromaDB collections, kb_documents, kb_document_shares | Write: Knowledge Service only<br>Read: Via Knowledge API |
| Agent Orchestrator | None | N/A (stateless) | No database ownership |
| Analytics Service | TimescaleDB | analytics_events, usage_metrics | Write: Analytics Service only<br>Read: Via Analytics API |

### 4.2 Cross-Service Data Access Rules

**Rule 1**: Never query another service's database directly

```python
# ❌ BAD: Direct database query
case = db.query(Case).filter(Case.case_id == case_id).first()

# ✅ GOOD: API call
case = await case_client.get_case(case_id)
```

**Rule 2**: Use events for eventual consistency

```python
# When user deleted in Auth Service:
async def delete_user(user_id: str):
    # 1. Delete from auth database
    await auth_repo.delete_user(user_id)

    # 2. Publish event
    await event_bus.publish(Event(
        type="auth.user.deleted",
        payload={"user_id": user_id}
    ))

# Case Service subscribes:
@event_handler("auth.user.deleted")
async def handle_user_deleted(event):
    # Delete user's cases asynchronously
    await case_repo.delete_cases_by_user(event.payload["user_id"])
```

**Rule 3**: Cache judiciously with TTL

```python
# Agent Orchestrator caching user profile
@cache(ttl=300)  # 5 minute cache
async def get_user_profile(user_id: str):
    return await auth_client.get_user(user_id)
```

### 4.3 Data Consistency Patterns

#### Pattern 1: Synchronous Consistency (Strong)
Use for critical operations requiring immediate consistency.

```python
# Create case (must succeed atomically)
async def create_case_with_session(user_id: str, title: str):
    # 1. Validate user (auth service call)
    user = await auth_client.get_user(user_id)
    if not user:
        raise UserNotFoundError()

    # 2. Create case (case service)
    case = await case_client.create_case(title=title, owner_id=user_id)

    # 3. Create session (session service)
    try:
        session = await session_client.create_session(
            user_id=user_id,
            case_id=case.case_id
        )
    except Exception:
        # Rollback: delete case
        await case_client.delete_case(case.case_id)
        raise

    return case, session
```

#### Pattern 2: Eventual Consistency (Events)
Use for non-critical operations that can be asynchronous.

```python
# Delete user (eventual consistency across services)
async def delete_user(user_id: str):
    # 1. Delete from auth database immediately
    await auth_repo.delete_user(user_id)

    # 2. Publish event (other services handle async)
    await event_bus.publish(Event(
        type="auth.user.deleted",
        payload={"user_id": user_id},
        correlation_id=generate_correlation_id()
    ))
    # Cases, sessions, KB docs will be deleted eventually
```

#### Pattern 3: Saga Pattern (Distributed Transactions)
Use for complex multi-service workflows.

```python
# Investigation workflow saga
class InvestigationSaga:
    async def start_investigation(self, case_id: str):
        # Step 1: Get case details
        case = await case_client.get_case(case_id)

        # Step 2: Search knowledge base
        try:
            kb_results = await kb_client.search(case.title)
        except Exception as e:
            # Compensating action: log failure
            await analytics_client.log_kb_failure(case_id)
            kb_results = []  # Continue with degraded data

        # Step 3: Generate hypotheses
        try:
            hypotheses = await investigation_client.generate_hypotheses(
                case_id=case_id,
                evidence=kb_results
            )
        except Exception as e:
            # Compensating action: create manual hypothesis placeholder
            hypotheses = [{"description": "Manual investigation required", "auto_generated": False}]

        # Step 4: Update case status
        await case_client.update_status(case_id, status="INVESTIGATING")

        return hypotheses
```

---

#### Pattern 4: Outbox/Inbox Pattern (Exactly-Once Events)
Use for reliable event delivery with exactly-once semantics without distributed transactions.

**Problem**: Publishing events and updating database atomically is hard
- Writing to DB succeeds, publishing event fails → lost event
- Publishing event succeeds, DB write fails → duplicate event
- Both succeed but service crashes → event published twice

**Solution**: Outbox pattern with idempotency keys

**Outbox Table** (in service database):
```sql
-- fm-case-service database
CREATE TABLE event_outbox (
    outbox_id BIGSERIAL PRIMARY KEY,
    event_id UUID UNIQUE NOT NULL DEFAULT gen_random_uuid(),
    event_type VARCHAR(100) NOT NULL,
    aggregate_id VARCHAR(50) NOT NULL,  -- e.g., case_id
    aggregate_type VARCHAR(50) NOT NULL,  -- e.g., 'case'
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    published BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_outbox_unpublished ON event_outbox(published, created_at) WHERE NOT published;
```

**Inbox Table** (in consuming service database):
```sql
-- fm-knowledge-service database
CREATE TABLE event_inbox (
    inbox_id BIGSERIAL PRIMARY KEY,
    event_id UUID UNIQUE NOT NULL,  -- Idempotency key!
    event_type VARCHAR(100) NOT NULL,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    processed BOOLEAN NOT NULL DEFAULT FALSE,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

CREATE UNIQUE INDEX idx_inbox_event_id ON event_inbox(event_id);  -- Prevent duplicates
CREATE INDEX idx_inbox_unprocessed ON event_inbox(processed, received_at) WHERE NOT processed;
```

**Dead Letter Queue** (for poison messages):
```sql
-- Shared DLQ table
CREATE TABLE event_dead_letter_queue (
    dlq_id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    payload JSONB NOT NULL,
    original_error TEXT NOT NULL,
    retry_count INTEGER NOT NULL,
    failed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    quarantined_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Publishing Pattern** (Outbox):
```python
# Case Service: Publish event transactionally
async def delete_case(case_id: str):
    async with db.transaction():
        # 1. Delete case from database
        await db.execute("DELETE FROM cases WHERE case_id = $1", case_id)

        # 2. Insert event into outbox (SAME TRANSACTION)
        await db.execute("""
            INSERT INTO event_outbox (event_type, aggregate_id, aggregate_type, payload)
            VALUES ($1, $2, $3, $4)
        """, "case.deleted", case_id, "case", json.dumps({"case_id": case_id}))

    # Transaction committed atomically - event guaranteed to be published eventually
```

**Outbox Publisher** (Background worker):
```python
# Separate background worker polls outbox and publishes to event bus
import asyncio

async def outbox_publisher():
    """Poll outbox table and publish events to Kafka/RabbitMQ"""
    while True:
        # Fetch unpublished events (batch of 100)
        events = await db.fetch("""
            SELECT outbox_id, event_id, event_type, aggregate_id, payload
            FROM event_outbox
            WHERE NOT published
            ORDER BY created_at
            LIMIT 100
            FOR UPDATE SKIP LOCKED
        """)

        for event in events:
            try:
                # Publish to event bus (Kafka/RabbitMQ)
                await event_bus.publish(
                    topic=event['event_type'],
                    key=event['aggregate_id'],
                    value=event['payload'],
                    headers={"event-id": str(event['event_id'])}  # Idempotency key
                )

                # Mark as published
                await db.execute("""
                    UPDATE event_outbox
                    SET published = TRUE, published_at = NOW()
                    WHERE outbox_id = $1
                """, event['outbox_id'])

                logger.info(f"Published event {event['event_id']}")

            except Exception as e:
                logger.error(f"Failed to publish event {event['event_id']}: {e}")
                # Event will be retried on next poll

        await asyncio.sleep(1)  # Poll every second
```

**Consuming Pattern** (Inbox with Idempotency):
```python
# Knowledge Service: Consume event idempotently
async def handle_case_deleted_event(event: dict):
    """Handle case.deleted event - delete case evidence from ChromaDB"""
    event_id = event['headers']['event-id']
    case_id = event['payload']['case_id']

    # Check if already processed (idempotency)
    existing = await db.fetch_one("""
        SELECT inbox_id FROM event_inbox WHERE event_id = $1
    """, event_id)

    if existing:
        logger.info(f"Event {event_id} already processed, skipping")
        return  # Idempotent - safe to skip

    try:
        # Insert into inbox (marks event as received)
        await db.execute("""
            INSERT INTO event_inbox (event_id, event_type, payload)
            VALUES ($1, $2, $3)
        """, event_id, "case.deleted", json.dumps(event['payload']))

        # Process event
        await chroma_client.delete_collection(f"case_evidence_{case_id}")

        # Mark as processed
        await db.execute("""
            UPDATE event_inbox
            SET processed = TRUE, processed_at = NOW()
            WHERE event_id = $1
        """, event_id)

        logger.info(f"Processed event {event_id} - deleted case {case_id} evidence")

    except Exception as e:
        # Increment retry count
        retry_count = await db.fetch_val("""
            UPDATE event_inbox
            SET retry_count = retry_count + 1, last_error = $2
            WHERE event_id = $1
            RETURNING retry_count
        """, event_id, str(e))

        if retry_count >= 5:
            # Move to Dead Letter Queue (quarantine poison message)
            await db.execute("""
                INSERT INTO event_dead_letter_queue (event_id, event_type, payload, original_error, retry_count)
                VALUES ($1, $2, $3, $4, $5)
            """, event_id, "case.deleted", json.dumps(event['payload']), str(e), retry_count)

            logger.error(f"Event {event_id} moved to DLQ after {retry_count} retries")
        else:
            logger.warning(f"Event {event_id} failed, will retry (attempt {retry_count}/5)")
```

**Replay from Dead Letter Queue**:
```python
# Admin tool to replay DLQ events after fixing issues
async def replay_dlq_event(dlq_id: int):
    """Manually replay event from DLQ after root cause fixed"""
    event = await db.fetch_one("""
        SELECT event_id, event_type, payload
        FROM event_dead_letter_queue
        WHERE dlq_id = $1
    """, dlq_id)

    # Re-publish to event bus
    await event_bus.publish(
        topic=event['event_type'],
        value=json.loads(event['payload']),
        headers={"event-id": str(event['event_id']), "replayed": "true"}
    )

    logger.info(f"Replayed DLQ event {dlq_id}")
```

**Outbox Cleanup** (archive old published events):
```sql
-- Daily cron job to archive published events older than 30 days
INSERT INTO event_outbox_archive
SELECT * FROM event_outbox
WHERE published = TRUE AND published_at < NOW() - INTERVAL '30 days';

DELETE FROM event_outbox
WHERE published = TRUE AND published_at < NOW() - INTERVAL '30 days';
```

**Benefits**:
- ✅ **Exactly-once semantics** via idempotency keys (event_id)
- ✅ **Atomic DB + event publishing** (single transaction)
- ✅ **Automatic retry** (outbox poller retries failed publishes)
- ✅ **Poison message handling** (DLQ quarantine + manual replay)
- ✅ **No message loss** (events persisted in DB, not just in-memory queue)
- ✅ **Audit trail** (inbox tracks all received events)

---

## 5. Service Contract Specifications

### 5.1 OpenAPI Contract Template

Every service must have an OpenAPI 3.1 specification in `{service-repo}/openapi/api.yaml`.

**Example: Case Service Contract**

```yaml
# fm-case-service/openapi/case-api.yaml
openapi: 3.1.0
info:
  title: FaultMaven Case Service API
  version: 1.0.0
  description: |
    Case lifecycle management service for FaultMaven enterprise platform.

    **Responsibilities**:
    - Case CRUD operations
    - Case status management
    - Case-session association
    - Case message history

    **Data Ownership**:
    - cases, case_messages, case_status_transitions, case_tags tables
  contact:
    name: FaultMaven Engineering
    email: engineering@faultmaven.com

servers:
  - url: https://api.faultmaven.com/case/v1
    description: Production
  - url: https://staging-api.faultmaven.com/case/v1
    description: Staging
  - url: http://localhost:8001/v1
    description: Local development

paths:
  /cases:
    post:
      operationId: createCase
      summary: Create a new troubleshooting case
      tags: [Cases]
      security:
        - BearerAuth: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CaseCreateRequest'
      responses:
        '201':
          description: Case created successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CaseResponse'
        '400':
          $ref: '#/components/responses/BadRequest'
        '401':
          $ref: '#/components/responses/Unauthorized'
        '429':
          $ref: '#/components/responses/RateLimited'

    get:
      operationId: listCases
      summary: List cases for authenticated user
      tags: [Cases]
      security:
        - BearerAuth: []
      parameters:
        - name: status
          in: query
          schema:
            type: string
            enum: [consulting, investigating, resolved, closed]
        - name: limit
          in: query
          schema:
            type: integer
            minimum: 1
            maximum: 100
            default: 20
        - name: offset
          in: query
          schema:
            type: integer
            minimum: 0
            default: 0
      responses:
        '200':
          description: List of cases
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CaseListResponse'

  /cases/{case_id}:
    get:
      operationId: getCase
      summary: Get case by ID
      tags: [Cases]
      security:
        - BearerAuth: []
      parameters:
        - name: case_id
          in: path
          required: true
          schema:
            type: string
            pattern: '^case_[a-z0-9]{13}$'
      responses:
        '200':
          description: Case details
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CaseResponse'
        '404':
          $ref: '#/components/responses/NotFound'

    put:
      operationId: updateCase
      summary: Update case details
      tags: [Cases]
      security:
        - BearerAuth: []
      parameters:
        - name: case_id
          in: path
          required: true
          schema:
            type: string
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CaseUpdateRequest'
      responses:
        '200':
          description: Case updated
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CaseResponse'

    delete:
      operationId: deleteCase
      summary: Delete case
      tags: [Cases]
      security:
        - BearerAuth: []
      parameters:
        - name: case_id
          in: path
          required: true
          schema:
            type: string
      responses:
        '204':
          description: Case deleted
        '404':
          $ref: '#/components/responses/NotFound'

  /cases/{case_id}/messages:
    post:
      operationId: addMessage
      summary: Add message to case conversation
      tags: [Messages]
      security:
        - BearerAuth: []
      parameters:
        - name: case_id
          in: path
          required: true
          schema:
            type: string
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/MessageCreateRequest'
      responses:
        '201':
          description: Message added
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/MessageResponse'

    get:
      operationId: listMessages
      summary: List case messages
      tags: [Messages]
      security:
        - BearerAuth: []
      parameters:
        - name: case_id
          in: path
          required: true
          schema:
            type: string
        - name: limit
          in: query
          schema:
            type: integer
            default: 50
        - name: offset
          in: query
          schema:
            type: integer
            default: 0
      responses:
        '200':
          description: List of messages
          content:
            application/json:
              schema:
                type: object
                properties:
                  messages:
                    type: array
                    items:
                      $ref: '#/components/schemas/MessageResponse'
                  total:
                    type: integer

components:
  securitySchemes:
    BearerAuth:
      type: http
      scheme: bearer
      bearerFormat: JWT
      description: JWT token from Auth Service

  schemas:
    CaseCreateRequest:
      type: object
      required:
        - title
      properties:
        title:
          type: string
          minLength: 1
          maxLength: 200
          example: "Production database connection timeout"
        description:
          type: string
          maxLength: 5000
          example: "Users reporting intermittent 500 errors"
        session_id:
          type: string
          pattern: '^session_[a-z0-9]+$'
        initial_message:
          type: string
          maxLength: 10000

    CaseResponse:
      type: object
      properties:
        case_id:
          type: string
          example: "case_abc123def4567"
        user_id:
          type: string
        title:
          type: string
        status:
          type: string
          enum: [consulting, investigating, resolved, closed]
        created_at:
          type: string
          format: date-time
        updated_at:
          type: string
          format: date-time
        metadata:
          type: object
          additionalProperties: true

    CaseUpdateRequest:
      type: object
      properties:
        title:
          type: string
          minLength: 1
          maxLength: 200
        status:
          type: string
          enum: [consulting, investigating, resolved, closed]

    MessageCreateRequest:
      type: object
      required:
        - content
        - role
      properties:
        content:
          type: string
          minLength: 1
          maxLength: 50000
        role:
          type: string
          enum: [user, assistant, system]

    MessageResponse:
      type: object
      properties:
        message_id:
          type: string
        case_id:
          type: string
        role:
          type: string
          enum: [user, assistant, system]
        content:
          type: string
        timestamp:
          type: string
          format: date-time

    CaseListResponse:
      type: object
      properties:
        cases:
          type: array
          items:
            $ref: '#/components/schemas/CaseResponse'
        total:
          type: integer
        limit:
          type: integer
        offset:
          type: integer

  responses:
    BadRequest:
      description: Invalid request
      content:
        application/json:
          schema:
            type: object
            properties:
              error:
                type: string
              details:
                type: object

    Unauthorized:
      description: Unauthorized
      content:
        application/json:
          schema:
            type: object
            properties:
              error:
                type: string
                example: "Invalid or expired token"

    NotFound:
      description: Resource not found
      content:
        application/json:
          schema:
            type: object
            properties:
              error:
                type: string
                example: "Case not found"

    RateLimited:
      description: Rate limit exceeded
      content:
        application/json:
          schema:
            type: object
            properties:
              error:
                type: string
              retry_after:
                type: integer
                description: Seconds until rate limit resets
```

### 5.2 AsyncAPI Event Contracts

**Event Bus Contract** (`fm-contracts/events/case-events.yaml`):

```yaml
asyncapi: 2.6.0
info:
  title: FaultMaven Case Events
  version: 1.0.0
  description: Event schemas for case lifecycle events

channels:
  case.created:
    description: Published when a new case is created
    publish:
      message:
        name: CaseCreated
        contentType: application/json
        payload:
          type: object
          required:
            - event_id
            - event_type
            - timestamp
            - correlation_id
            - payload
          properties:
            event_id:
              type: string
              format: uuid
              description: Unique event identifier
            event_type:
              type: string
              const: case.created
            timestamp:
              type: string
              format: date-time
            correlation_id:
              type: string
              description: Request correlation ID for tracing
            payload:
              type: object
              required:
                - case_id
                - user_id
                - title
                - status
              properties:
                case_id:
                  type: string
                user_id:
                  type: string
                title:
                  type: string
                status:
                  type: string
                  enum: [consulting, investigating, resolved, closed]
                created_at:
                  type: string
                  format: date-time

  case.status_changed:
    description: Published when case status changes
    publish:
      message:
        name: CaseStatusChanged
        contentType: application/json
        payload:
          type: object
          properties:
            event_id:
              type: string
              format: uuid
            event_type:
              type: string
              const: case.status_changed
            timestamp:
              type: string
              format: date-time
            correlation_id:
              type: string
            payload:
              type: object
              properties:
                case_id:
                  type: string
                user_id:
                  type: string
                from_status:
                  type: string
                to_status:
                  type: string
                reason:
                  type: string

  case.deleted:
    description: Published when case is deleted
    publish:
      message:
        name: CaseDeleted
        contentType: application/json
        payload:
          type: object
          properties:
            event_id:
              type: string
            event_type:
              type: string
              const: case.deleted
            timestamp:
              type: string
              format: date-time
            correlation_id:
              type: string
            payload:
              type: object
              properties:
                case_id:
                  type: string
                user_id:
                  type: string
```

### 5.3 Contract Testing Strategy

**Consumer-Driven Contracts** using Pact:

```python
# fm-agent-service/tests/contract/test_case_service_contract.py
import pytest
from pact import Consumer, Provider

@pytest.fixture
def pact():
    return Consumer('agent-service').has_pact_with(Provider('case-service'))

def test_get_case_contract(pact):
    expected_response = {
        "case_id": "case_abc123",
        "user_id": "user_xyz789",
        "title": "Database timeout",
        "status": "investigating",
        "created_at": "2025-11-14T10:00:00Z"
    }

    (pact
     .given('case exists with ID case_abc123')
     .upon_receiving('a request for case details')
     .with_request('GET', '/v1/cases/case_abc123', headers={'Authorization': 'Bearer valid-token'})
     .will_respond_with(200, body=expected_response))

    with pact:
        # Run actual integration test
        case = case_client.get_case('case_abc123')
        assert case['case_id'] == 'case_abc123'
        assert case['status'] == 'investigating'
```

**Automated Contract Verification** in CI/CD:

```yaml
# .github/workflows/contract-tests.yml
name: Contract Tests
on: [pull_request]

jobs:
  contract-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run consumer contract tests
        run: |
          pytest tests/contract/ --pact-broker-url=${{ secrets.PACT_BROKER_URL }}

      - name: Verify provider contracts
        run: |
          pact-verifier --provider-base-url=http://localhost:8001 \
                        --pact-broker-url=${{ secrets.PACT_BROKER_URL }} \
                        --provider-name=case-service
```

---

### 5.4 Event Schema Versioning & Evolution

**Problem**: Event schemas evolve over time, but consumers expect stability

**Solution**: Semantic versioning for event schemas with compatibility rules

**Versioned Event Schema**:
```yaml
# fm-contracts/events/case-events-v1.yaml
asyncapi: 2.6.0
info:
  title: FaultMaven Case Events
  version: 1.0.0  # Schema version

channels:
  case.created.v1:  # Version in channel name
    publish:
      message:
        name: CaseCreatedV1
        schemaFormat: application/schema+json;version=draft-07
        payload:
          $id: https://faultmaven.com/schemas/case.created.v1.json
          $schema: http://json-schema.org/draft-07/schema#
          type: object
          required: [event_id, event_type, schema_version, payload]
          properties:
            event_id:
              type: string
              format: uuid
            event_type:
              type: string
              const: case.created
            schema_version:
              type: string
              const: "1.0.0"  # Explicit schema version
            payload:
              type: object
              required: [case_id, user_id, title]
              properties:
                case_id:
                  type: string
                user_id:
                  type: string
                title:
                  type: string
                status:
                  type: string
                  default: "consulting"  # Optional with default
```

**Evolution Example** (v2 adds new field):
```yaml
# fm-contracts/events/case-events-v2.yaml
channels:
  case.created.v2:  # New version channel
    publish:
      message:
        name: CaseCreatedV2
        payload:
          $id: https://faultmaven.com/schemas/case.created.v2.json
          type: object
          properties:
            schema_version:
              const: "2.0.0"
            payload:
              required: [case_id, user_id, title, organization_id]  # NEW required field
              properties:
                organization_id:  # NEW field in v2
                  type: string
                  description: "Organization ID (added in v2.0.0)"
```

**Compatibility Rules**:

| Change Type | Backward Compatible? | Forward Compatible? | Action |
|-------------|---------------------|---------------------|--------|
| Add optional field | ✅ Yes | ✅ Yes | Minor version bump (1.0 → 1.1) |
| Add required field | ❌ No | ✅ Yes | Major version bump (1.0 → 2.0) |
| Remove field | ❌ No | ❌ No | Major version bump + deprecation |
| Rename field | ❌ No | ❌ No | Major version bump (publish both versions) |
| Change field type | ❌ No | ❌ No | Major version bump |

**Publishing Multiple Versions**:
```python
# Publish both v1 and v2 events during transition period
async def publish_case_created(case: Case):
    # Publish v1 (for legacy consumers)
    await event_bus.publish(
        topic="case.created.v1",
        value={
            "event_id": str(uuid.uuid4()),
            "event_type": "case.created",
            "schema_version": "1.0.0",
            "payload": {
                "case_id": case.case_id,
                "user_id": case.user_id,
                "title": case.title,
                "status": case.status
            }
        }
    )

    # ALSO publish v2 (for new consumers)
    await event_bus.publish(
        topic="case.created.v2",
        value={
            "event_id": str(uuid.uuid4()),
            "event_type": "case.created",
            "schema_version": "2.0.0",
            "payload": {
                "case_id": case.case_id,
                "user_id": case.user_id,
                "title": case.title,
                "status": case.status,
                "organization_id": case.organization_id  # NEW field
            }
        }
    )
```

**Consumer Migration**:
```python
# Consumer subscribes to both v1 and v2, migrates gradually
async def handle_case_created(message):
    schema_version = message['schema_version']

    if schema_version == "1.0.0":
        # Handle v1 schema
        case_id = message['payload']['case_id']
        org_id = None  # Not available in v1

    elif schema_version == "2.0.0":
        # Handle v2 schema
        case_id = message['payload']['case_id']
        org_id = message['payload']['organization_id']

    else:
        logger.error(f"Unknown schema version: {schema_version}")
        return

    # Process with normalized data
    await process_case(case_id, org_id)
```

**Schema Deprecation Policy**:
- **Announce**: 3 months before deprecation
- **Dual-Publish**: Publish both old and new versions for 6 months
- **Sunset**: Stop publishing old version after 6 months
- **Archive**: Keep schema documentation for 2 years

---

### 5.5 API Lifecycle & Deprecation Policy

**N+2 Deprecation Rule**: API version N must be supported until N+2 is released

```
v1 (current) → v2 released → v3 released → v1 deprecated
├─────────────┼─────────────┼─────────────┼────────────┤
│   Active    │   Active    │ Deprecated  │  Sunset    │
│             │  (v1, v2)   │  (v1, v2)   │  (v2, v3)  │
```

**Version Lifecycle States**:

| State | Description | Support Level | Duration |
|-------|-------------|---------------|----------|
| **Active** | Current stable version | Full support | Until N+2 |
| **Deprecated** | Marked for removal | Security fixes only | 6-12 months |
| **Sunset** | No longer supported | None | Archived |

**Deprecation Headers**:
```python
# API response includes deprecation warnings
@app.get("/v1/cases/{case_id}")
async def get_case_v1(case_id: str, response: Response):
    # Add deprecation header
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "2026-06-01T00:00:00Z"  # RFC 8594
    response.headers["Link"] = '</v2/cases/{case_id}>; rel="successor-version"'

    # Log deprecation usage for monitoring
    logger.warning(f"Deprecated endpoint /v1/cases called (sunset 2026-06-01)")

    # Return data
    return await case_service.get_case(case_id)
```

**Breaking Change Detection in CI**:
```yaml
# .github/workflows/breaking-change-guard.yml
name: Breaking Change Detection

on: [pull_request]

jobs:
  check-breaking-changes:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # Fetch all history

      - name: Check OpenAPI breaking changes
        uses: oasdiff/oasdiff-action@v0.0.15
        with:
          base: main
          revision: HEAD
          fail-on-diff: true
          fail-on: ERR  # Fail on breaking changes only

      - name: Generate breaking change report
        if: failure()
        run: |
          oasdiff breaking openapi/api.yaml openapi/api-old.yaml \
            -f markdown > breaking-changes.md

      - name: Comment on PR
        if: failure()
        uses: marocchino/sticky-pull-request-comment@v2
        with:
          path: breaking-changes.md
          header: "⚠️ Breaking Changes Detected"
```

**Breaking Change Examples**:

```yaml
# ❌ BREAKING: Removing required field
# OLD (v1)
components:
  schemas:
    CaseResponse:
      required: [case_id, title, user_id]

# NEW (v2) - user_id removed (BREAKING!)
components:
  schemas:
    CaseResponse:
      required: [case_id, title]  # Missing user_id

# ✅ SOLUTION: Keep user_id, add new version
```

```yaml
# ❌ BREAKING: Changing response type
# OLD
responses:
  '200':
    content:
      application/json:
        schema:
          type: array  # Array of cases

# NEW (BREAKING!)
responses:
  '200':
    content:
      application/json:
        schema:
          type: object  # Changed to paginated object
          properties:
            cases:
              type: array

# ✅ SOLUTION: Create v2 endpoint with pagination
```

**Non-Breaking Changes** (safe):
```yaml
# ✅ SAFE: Adding optional field
properties:
  organization_id:
    type: string
    description: "Organization ID (optional, added in v1.1)"

# ✅ SAFE: Adding new optional query parameter
parameters:
  - name: include_archived
    in: query
    required: false
    schema:
      type: boolean
      default: false

# ✅ SAFE: Adding new response status code
responses:
  '201':
    description: Case created (new in v1.2)
```

**Sunset Schedule** (example timeline):
```
2025-01-15: v1 released (active)
2025-06-01: v2 released (v1 active, v2 active)
2025-12-01: v3 released (v1 deprecated, v2 active, v3 active)
2026-06-01: v1 sunset (v2 active, v3 active)
2026-12-01: v4 released (v2 deprecated, v3 active, v4 active)
2027-06-01: v2 sunset (v3 active, v4 active)
```

**Monitoring Deprecated APIs**:
```python
# Prometheus metrics for deprecated API usage
deprecated_api_calls = Counter(
    'deprecated_api_calls_total',
    'Total calls to deprecated API endpoints',
    ['endpoint', 'version', 'client_id']
)

@app.middleware("http")
async def track_deprecated_usage(request: Request, call_next):
    if request.url.path.startswith("/v1/"):
        # Track deprecated v1 usage
        deprecated_api_calls.labels(
            endpoint=request.url.path,
            version="v1",
            client_id=request.headers.get("X-Client-ID", "unknown")
        ).inc()

    return await call_next(request)
```

**Client Migration Guide**:
```markdown
# API v1 to v2 Migration Guide

## Sunset Date: 2026-06-01

### Breaking Changes

1. **Pagination Required**
   - Old: `GET /v1/cases` returns array
   - New: `GET /v2/cases` returns paginated object
   - Migration: Update client to handle `{cases: [], total: N}` format

2. **Required `organization_id`**
   - Old: `POST /v1/cases` without organization_id
   - New: `POST /v2/cases` requires organization_id
   - Migration: Add organization_id to all case creation requests

### Migration Timeline

- 2025-12-01: v3 released, v1 deprecated
- 2026-03-01: Email reminders to v1 users
- 2026-05-01: Final warning (1 month before sunset)
- 2026-06-01: v1 sunset (503 Service Unavailable returned)
```

---

## 6. Strangler Pattern Migration Strategy

### 6.1 Strangler Pattern Overview

**Concept**: Gradually replace monolithic functionality with microservices by routing traffic through a facade/gateway.

```
Phase 1: Monolith Handles Everything
┌─────────┐
│ Client  │
└────┬────┘
     │
     v
┌─────────────────┐
│   Monolith      │
│  (All Features) │
└─────────────────┘

Phase 2: Route /auth to New Service
┌─────────┐
│ Client  │
└────┬────┘
     │
     v
┌──────────────────┐
│  API Gateway     │
└──┬───────────┬───┘
   │           │
   │ /auth/*   │ /*
   v           v
┌─────────┐ ┌─────────────┐
│  Auth   │ │  Monolith   │
│ Service │ │  (minus     │
│         │ │   auth)     │
└─────────┘ └─────────────┘

Phase 3: More Services Extracted
┌─────────┐
│ Client  │
└────┬────┘
     │
     v
┌──────────────────────────┐
│  API Gateway             │
└┬────┬─────┬──────┬───────┘
 │    │     │      │
 │auth│case │session│ (remaining)
 v    v     v      v
┌───┐┌───┐┌────┐┌──────┐
│Aut││Cas││Sess││Monol │
│h  ││e  ││ion ││ith   │
└───┘└───┘└────┘└──────┘

Phase 4: Monolith Fully Replaced
┌─────────┐
│ Client  │
└────┬────┘
     │
     v
┌──────────────────────────┐
│  API Gateway             │
└┬───┬───┬────┬────┬───────┘
 │   │   │    │    │
 v   v   v    v    v
Auth Case Sess KB  Agent
```

### 6.2 Service Extraction Procedure (Ports & Adapters)

**For Each Service to Extract**, follow this 7-step procedure:

---

#### Step 1: Stabilize Boundary Inside Monolith

**Objective**: Create clean interface inside monolith before extraction

**Actions**:
```python
# BEFORE: Direct model access scattered throughout codebase
from faultmaven.infrastructure.persistence.case_repository import CaseRepository

# In controller
case = CaseRepository().get_case(case_id)

# AFTER: Clean service boundary with DTOs
from faultmaven.services.domain.case_service import CaseService
from faultmaven.models.api_models import CaseResponse

# In controller
case_service = CaseService()  # Injected via DI
case_dto = await case_service.get_case(case_id)  # Returns DTO, not DB model
```

**Checklist**:
- [ ] Wrap target module behind a service interface
- [ ] Define DTOs (Data Transfer Objects) for all boundaries
- [ ] Block direct access to database models (use DTOs only)
- [ ] Add integration tests for the service boundary
- [ ] Refactor monolith code to use service interface

---

#### Step 2: Introduce Ports & Adapters

**Objective**: Separate domain logic from infrastructure

**Define Service Port** (use cases):
```python
# faultmaven/services/domain/ports/case_port.py
from abc import ABC, abstractmethod
from typing import List
from faultmaven.models.api_models import CaseResponse, CaseCreateRequest

class CasePort(ABC):
    """Port defining case service use cases (domain interface)"""

    @abstractmethod
    async def create_case(self, request: CaseCreateRequest) -> CaseResponse:
        """Create a new case"""
        pass

    @abstractmethod
    async def get_case(self, case_id: str) -> CaseResponse:
        """Get case by ID"""
        pass

    @abstractmethod
    async def list_cases(self, user_id: str) -> List[CaseResponse]:
        """List user's cases"""
        pass
```

**Define Persistence Port** (repository interface):
```python
# faultmaven/services/domain/ports/case_repository_port.py
from abc import ABC, abstractmethod
from faultmaven.models.case import Case

class CaseRepositoryPort(ABC):
    """Port defining case persistence operations"""

    @abstractmethod
    async def save(self, case: Case) -> Case:
        """Persist case to storage"""
        pass

    @abstractmethod
    async def find_by_id(self, case_id: str) -> Case:
        """Retrieve case by ID"""
        pass

    @abstractmethod
    async def find_by_user(self, user_id: str) -> List[Case]:
        """Find all cases for user"""
        pass
```

**Implement Adapters**:
```python
# faultmaven/infrastructure/persistence/postgres_case_repository.py
from faultmaven.services.domain.ports.case_repository_port import CaseRepositoryPort

class PostgresCaseRepository(CaseRepositoryPort):
    """Adapter implementing persistence port using PostgreSQL"""

    async def save(self, case: Case) -> Case:
        # PostgreSQL-specific implementation
        async with self.db.transaction():
            result = await self.db.execute(
                "INSERT INTO cases (case_id, title, user_id) VALUES ($1, $2, $3)",
                case.case_id, case.title, case.user_id
            )
        return case
```

**Dependency Injection**:
```python
# faultmaven/container.py
from faultmaven.services.domain.ports.case_port import CasePort
from faultmaven.services.domain.ports.case_repository_port import CaseRepositoryPort
from faultmaven.infrastructure.persistence.postgres_case_repository import PostgresCaseRepository
from faultmaven.services.domain.case_service import CaseService

# Wire ports to adapters
case_repository: CaseRepositoryPort = PostgresCaseRepository()
case_service: CasePort = CaseService(repository=case_repository)
```

**Checklist**:
- [ ] Define service port (use case interface)
- [ ] Define repository port (persistence interface)
- [ ] Implement PostgreSQL adapter for repository
- [ ] Wire dependencies via DI container
- [ ] Update monolith to use ports (not concrete implementations)

---

#### Step 3: Externalize Persistence

**Objective**: Move database migrations to new service repo

**Create New Service Repository**:
```bash
# Create service repo
gh repo create FaultMaven/fm-case-service --private
cd fm-case-service

# Initialize structure
mkdir -p src/routes src/domain src/persistence migrations
```

**Move Migrations**:
```bash
# Copy migrations from monolith
cp faultmaven/migrations/versions/*_case_*.py fm-case-service/migrations/versions/

# Update migration scripts to use new database URL
# Edit migrations to point to fm_case database
```

**Create New Database**:
```bash
# Create dedicated case database
createdb fm_case

# Run migrations in new database
cd fm-case-service
alembic upgrade head
```

**Monolith Still Uses Port**:
```python
# Monolith continues to call CasePort interface
# But adapter can now point to:
#   - Old monolith database (during migration)
#   - New fm_case database (after migration)
#   - Remote Case Service API (after deployment)

# No change needed in monolith business logic!
```

**Checklist**:
- [ ] Create new service repository structure
- [ ] Copy database migrations to new repo
- [ ] Create new dedicated database
- [ ] Run migrations in new database
- [ ] Verify schema matches monolith schema

---

#### Step 4: Define Service Contract

**Objective**: Author OpenAPI spec before implementation

**Create OpenAPI Contract**:
```yaml
# fm-case-service/openapi/case-api.yaml
openapi: 3.1.0
info:
  title: Case Service API
  version: 1.0.0
paths:
  /v1/cases:
    post:
      operationId: createCase
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CaseCreateRequest'
      responses:
        '201':
          description: Case created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CaseResponse'
```

**Add Consumer Contract Tests** (in monolith):
```python
# faultmaven/tests/contract/test_case_service_contract.py
import pytest
from pact import Consumer, Provider

@pytest.fixture
def pact():
    return Consumer('faultmaven-monolith').has_pact_with(Provider('case-service'))

def test_create_case_contract(pact):
    """Verify monolith expectations match case service contract"""
    expected = {
        "case_id": "case_abc123",
        "title": "Database timeout",
        "status": "consulting"
    }

    (pact
     .given('user authenticated')
     .upon_receiving('create case request')
     .with_request('POST', '/v1/cases', body={"title": "Database timeout"})
     .will_respond_with(201, body=expected))

    with pact:
        # Monolith code calling case service
        case = await case_client.create_case(title="Database timeout")
        assert case['case_id'].startswith('case_')
```

**Checklist**:
- [ ] Author complete OpenAPI specification
- [ ] Add consumer contract tests in monolith
- [ ] Generate client SDK from OpenAPI spec
- [ ] Verify contract tests pass (mock server)

---

#### Step 5: Implement New Service

**Objective**: Build standalone service implementing the contract

**Service Structure**:
```
fm-case-service/
├── src/
│   ├── main.py              # FastAPI app
│   ├── routes/
│   │   └── cases.py         # API endpoints (implements OpenAPI)
│   ├── domain/
│   │   ├── models.py        # Domain entities
│   │   └── service.py       # Business logic
│   └── persistence/
│       └── repository.py    # Database access
├── openapi/
│   └── case-api.yaml
├── migrations/
│   └── versions/
│       └── 001_initial_schema.py
└── tests/
    ├── unit/
    ├── integration/
    └── contract/            # Provider-side contract verification
```

**Implement API Layer**:
```python
# fm-case-service/src/routes/cases.py
from fastapi import APIRouter, Depends
from src.domain.service import CaseService
from src.models import CaseCreateRequest, CaseResponse

router = APIRouter()

@router.post("/v1/cases", response_model=CaseResponse, status_code=201)
async def create_case(
    request: CaseCreateRequest,
    service: CaseService = Depends(get_case_service)
):
    """Create case - implements OpenAPI contract"""
    case = await service.create_case(request)
    return CaseResponse.from_domain(case)
```

**Add Provider Contract Verification**:
```python
# fm-case-service/tests/contract/test_provider_contract.py
import pytest
from pact_verifier import Verifier

def test_verify_pact_with_monolith():
    """Verify case service implements contract expected by monolith"""
    verifier = Verifier(
        provider='case-service',
        provider_base_url='http://localhost:8001'
    )

    # Retrieve pact from broker (or file)
    verifier.verify_pacts(
        './pacts/faultmaven-monolith-case-service.json',
        provider_states_setup_url='http://localhost:8001/_pact/provider-states'
    )
```

**Checklist**:
- [ ] Implement FastAPI application
- [ ] Implement domain logic (business rules)
- [ ] Implement persistence layer (repository)
- [ ] Add health/readiness endpoints
- [ ] Add metrics/tracing instrumentation
- [ ] Write unit tests (>80% coverage)
- [ ] Write integration tests (DB interactions)
- [ ] Add provider-side contract verification

---

#### Step 6: Dual-Read / Dual-Write Cutover

**Objective**: Migrate data safely with verification

**Phase 6a: Data Migration**
```python
# scripts/migrate_case_data.py
import asyncio
import asyncpg

async def migrate_cases():
    source = await asyncpg.connect('postgresql://monolith-db/faultmaven')
    target = await asyncpg.connect('postgresql://case-db/fm_case')

    # Copy cases in batches
    offset = 0
    batch_size = 1000

    while True:
        cases = await source.fetch(
            "SELECT * FROM cases ORDER BY created_at LIMIT $1 OFFSET $2",
            batch_size, offset
        )

        if not cases:
            break

        # Insert into new database
        for case in cases:
            await target.execute("""
                INSERT INTO cases (case_id, title, user_id, status, created_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (case_id) DO NOTHING
            """, case['case_id'], case['title'], case['user_id'],
                case['status'], case['created_at'])

        offset += batch_size
        print(f"Migrated {offset} cases")

    await source.close()
    await target.close()

asyncio.run(migrate_cases())
```

**Phase 6b: Dual-Write Period**
```python
# Monolith adapter during dual-write
class DualWriteCaseAdapter(CaseRepositoryPort):
    """Writes to BOTH monolith DB AND new case service"""

    def __init__(self, monolith_repo, case_service_client):
        self.monolith_repo = monolith_repo
        self.case_service_client = case_service_client

    async def save(self, case: Case) -> Case:
        # Write to monolith database
        result = await self.monolith_repo.save(case)

        # ALSO write to new case service
        try:
            await self.case_service_client.create_case({
                "case_id": case.case_id,
                "title": case.title,
                "user_id": case.user_id
            })
        except Exception as e:
            logger.error(f"Dual-write to case service failed: {e}")
            # Don't fail the request (graceful degradation)

        return result
```

**Phase 6c: Dual-Read with Diff**
```python
# Verify data consistency by comparing reads
class DualReadCaseAdapter(CaseRepositoryPort):
    """Reads from BOTH sources and compares for consistency"""

    async def find_by_id(self, case_id: str) -> Case:
        # Read from monolith
        monolith_case = await self.monolith_repo.find_by_id(case_id)

        # ALSO read from new service
        service_case = await self.case_service_client.get_case(case_id)

        # Compare for consistency
        if not cases_match(monolith_case, service_case):
            logger.error(f"Data inconsistency for case {case_id}")
            await alert_on_call("Case data mismatch detected")

        # Return monolith data (still authoritative during migration)
        return monolith_case
```

**Phase 6d: Switch Reads to Service**
```python
# After consistency verified, switch reads to service
class CaseServiceAdapter(CaseRepositoryPort):
    """Reads from case service, fallback to monolith on error"""

    async def find_by_id(self, case_id: str) -> Case:
        try:
            # Primary: Read from case service
            return await self.case_service_client.get_case(case_id)
        except Exception as e:
            logger.warning(f"Case service unavailable, falling back: {e}")
            # Fallback: Read from monolith
            return await self.monolith_repo.find_by_id(case_id)
```

**Phase 6e: Disable Monolith Writes**
```python
# After reads switched successfully, stop writing to monolith
class CaseServiceOnlyAdapter(CaseRepositoryPort):
    """Writes only to case service (monolith disabled)"""

    async def save(self, case: Case) -> Case:
        # Only write to case service
        await self.case_service_client.create_case(case.to_dict())
        return case
```

**Checklist**:
- [ ] Migrate historical data (backfill)
- [ ] Enable dual-write (both DBs)
- [ ] Monitor data consistency (dual-read with diff)
- [ ] Verify integrity (row counts, checksums)
- [ ] Switch reads to new service (with fallback)
- [ ] Monitor error rates (<1% errors)
- [ ] Disable monolith writes
- [ ] Run for 1 week before cleanup

---

#### Step 7: Kill the Shim (Decommission Monolith Code)

**Objective**: Remove monolith implementation completely

**Final Cleanup**:
```bash
# 1. Remove monolith database tables
psql faultmaven << EOF
-- Backup first!
CREATE TABLE cases_backup AS SELECT * FROM cases;

-- Drop tables
DROP TABLE case_tags CASCADE;
DROP TABLE case_status_transitions CASCADE;
DROP TABLE case_messages CASCADE;
DROP TABLE cases CASCADE;
EOF

# 2. Remove monolith code
rm -rf faultmaven/services/domain/case_service.py
rm -rf faultmaven/infrastructure/persistence/case_repository.py
rm -rf faultmaven/api/v1/routes/case.py

# 3. Update monolith to only use client
# faultmaven/adapters/case_service_client.py (keep this)
```

**Monolith Now Only Has**:
```python
# faultmaven/adapters/case_service_client.py
import httpx

class CaseServiceClient:
    """Client adapter for remote case service"""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.client = httpx.AsyncClient()

    async def create_case(self, request: dict) -> dict:
        response = await self.client.post(
            f"{self.base_url}/v1/cases",
            json=request,
            headers={"Authorization": f"Bearer {get_token()}"}
        )
        response.raise_for_status()
        return response.json()
```

**API Gateway Routes** (final state):
```yaml
# All /cases/* traffic goes to case service
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: case-service-routes
spec:
  rules:
  - host: api.faultmaven.com
    http:
      paths:
      - path: /cases
        pathType: Prefix
        backend:
          service:
            name: fm-case-service
            port:
              number: 8000
```

**Checklist**:
- [ ] Backup monolith database tables
- [ ] Drop tables from monolith database
- [ ] Delete monolith service code
- [ ] Delete monolith API routes
- [ ] Keep only client adapter in monolith
- [ ] Update API gateway to route 100% to service
- [ ] Monitor for 1 week
- [ ] Mark migration complete

---

**Repeat Steps 1-7 for Each Service** (Auth, Session, Case, Knowledge, Agent)

---

### 6.3 Migration Phases

#### Phase 1: Setup Infrastructure (Week 1)

**Deliverables**:
1. API Gateway deployment (Kong or NGINX Ingress)
2. Distributed tracing setup (Jaeger/Tempo)
3. Correlation ID middleware
4. Service discovery (Kubernetes DNS)

**API Gateway Configuration**:

```yaml
# kong/routes.yaml
apiVersion: configuration.konghq.com/v1
kind: KongIngress
metadata:
  name: faultmaven-routes
route:
  preserve_host: true
  protocols:
  - https
  methods:
  - GET
  - POST
  - PUT
  - DELETE

---
# Route auth requests to new service
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: auth-service
  annotations:
    konghq.com/plugins: rate-limiting, correlation-id, jwt-auth
spec:
  rules:
  - host: api.faultmaven.com
    http:
      paths:
      - path: /auth
        pathType: Prefix
        backend:
          service:
            name: fm-auth-service
            port:
              number: 8000

---
# Route everything else to monolith (for now)
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: monolith-catchall
spec:
  rules:
  - host: api.faultmaven.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: faultmaven-monolith
            port:
              number: 8000
```

**Correlation ID Middleware**:

```python
# kong/plugins/correlation-id.lua
local correlation_id = kong.request.get_header("X-Correlation-ID")
if not correlation_id then
  correlation_id = kong.utils.uuid()
  kong.service.request.set_header("X-Correlation-ID", correlation_id)
end
kong.response.set_header("X-Correlation-ID", correlation_id)
```

---

#### Phase 2: Extract Auth Service (Week 2-3)

**Step 1: Create Auth Service with Own Database**

```bash
# Create new PostgreSQL database for auth
createdb fm_auth

# Create service repository
gh repo create FaultMaven/fm-auth-service --private
cd fm-auth-service

# Initialize service structure
mkdir -p src/routes src/domain src/persistence migrations chart
```

**Step 2: Migrate Auth Tables**

```sql
-- migrations/001_create_auth_schema.sql
-- Copy tables from monolith
CREATE TABLE users (
    user_id VARCHAR(20) PRIMARY KEY,
    username VARCHAR(100) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE organizations (
    org_id VARCHAR(20) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,
    plan_tier VARCHAR(20) NOT NULL DEFAULT 'free',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ... (other auth tables)
```

**Step 3: Dual-Write Period**

```python
# Monolith code: Write to both old DB and new Auth Service
async def create_user(username: str, email: str, password: str):
    # 1. Write to monolith database (existing code)
    user = await db.create_user(username, email, password)

    # 2. ALSO write to new Auth Service
    try:
        await auth_service_client.create_user({
            "user_id": user.user_id,
            "username": username,
            "email": email,
            "password_hash": user.password_hash
        })
    except Exception as e:
        # Log but don't fail (dual-write can fail during migration)
        logger.error(f"Dual-write to auth service failed: {e}")

    return user
```

**Step 4: Data Migration Script**

```python
# scripts/migrate_auth_data.py
import asyncio
import asyncpg
from tqdm import tqdm

async def migrate_users():
    # Source: monolith database
    source_conn = await asyncpg.connect(
        host='monolith-db.internal',
        database='faultmaven',
        user='faultmaven'
    )

    # Target: auth service database
    target_conn = await asyncpg.connect(
        host='auth-db.internal',
        database='fm_auth',
        user='fm_auth'
    )

    # Fetch all users
    users = await source_conn.fetch('SELECT * FROM users')

    # Insert into auth database
    for user in tqdm(users, desc="Migrating users"):
        try:
            await target_conn.execute('''
                INSERT INTO users (user_id, username, email, password_hash, created_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id) DO NOTHING
            ''', user['user_id'], user['username'], user['email'],
                user['password_hash'], user['created_at'])
        except Exception as e:
            logger.error(f"Failed to migrate user {user['user_id']}: {e}")

    await source_conn.close()
    await target_conn.close()

if __name__ == '__main__':
    asyncio.run(migrate_users())
```

**Step 5: Gradual Traffic Shift**

```yaml
# Week 2: 10% traffic to new auth service
apiVersion: v1
kind: ConfigMap
metadata:
  name: auth-traffic-split
data:
  auth_service_weight: "10"
  monolith_weight: "90"

---
# Week 3: 50% traffic
apiVersion: v1
kind: ConfigMap
metadata:
  name: auth-traffic-split
data:
  auth_service_weight: "50"
  monolith_weight: "50"

---
# Week 4: 100% traffic (monolith auth routes disabled)
apiVersion: v1
kind: ConfigMap
metadata:
  name: auth-traffic-split
data:
  auth_service_weight: "100"
  monolith_weight: "0"
```

**Step 6: Decommission Monolith Auth Code**

```python
# Monolith: Remove auth routes after 100% traffic cutover
# api/v1/routes/auth.py - DELETE THIS FILE
# infrastructure/persistence/user_repository.py - DELETE THIS FILE

# Update API Gateway to return 410 Gone for old auth endpoints
```

---

#### Phase 3: Extract Session Service (Week 4-5)

**Simpler than Auth** (Redis-only, no database migration):

**Step 1: Create Session Service**

```python
# fm-session-service/src/main.py
from fastapi import FastAPI
from redis import asyncio as aioredis

app = FastAPI()
redis = aioredis.from_url("redis://fm-redis-cluster:6379")

@app.post("/v1/sessions")
async def create_session(user_id: str):
    session_id = generate_session_id()
    await redis.setex(
        f"session:{session_id}",
        86400,  # 24 hour TTL
        json.dumps({"user_id": user_id, "created_at": now()})
    )
    return {"session_id": session_id}

@app.get("/v1/sessions/{session_id}")
async def get_session(session_id: str):
    data = await redis.get(f"session:{session_id}")
    if not data:
        raise HTTPException(404, "Session not found")
    return json.loads(data)
```

**Step 2: Update Gateway**

```yaml
# Route /sessions to new service
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: session-service
spec:
  rules:
  - host: api.faultmaven.com
    http:
      paths:
      - path: /sessions
        pathType: Prefix
        backend:
          service:
            name: fm-session-service
            port:
              number: 8000
```

**Step 3: No Dual-Write Needed** (Redis is already shared, just change which service accesses it)

---

#### Phase 4: Extract Case Service (Week 6-8)

**Most Complex** (large database, many dependencies):

**Step 1: Analyze Dependencies**

```bash
# Find all code that accesses cases table
rg "cases\." faultmaven/ --type py

# Identify services that need case data
# - Agent Service (reads case details)
# - Analytics Service (reads case stats)
# - Investigation Service (reads case context)
```

**Step 2: Create Case Service API First**

```yaml
# fm-case-service/openapi/api.yaml (see section 5.1 for full contract)
```

**Step 3: Implement Service**

```python
# fm-case-service/src/routes/cases.py
from fastapi import APIRouter
from src.domain.case_repository import CaseRepository

router = APIRouter()
case_repo = CaseRepository()

@router.post("/v1/cases")
async def create_case(request: CaseCreateRequest, user_id: str = Depends(get_current_user)):
    case = await case_repo.create(
        title=request.title,
        user_id=user_id,
        description=request.description
    )

    # Publish event
    await event_bus.publish(Event(
        type="case.created",
        payload={"case_id": case.case_id, "user_id": user_id}
    ))

    return case
```

**Step 4: Migrate Data**

```bash
# Export cases from monolith
pg_dump -h monolith-db -t cases -t case_messages -t case_status_transitions -t case_tags \
        --data-only --column-inserts faultmaven > cases_export.sql

# Import to case service DB
psql -h case-db fm_case < cases_export.sql
```

**Step 5: Dual-Write Period** (1 week)

```python
# Monolith: Write to both old table and new Case Service API
async def create_case_monolith(title: str, user_id: str):
    # 1. Write to monolith DB
    case_id = generate_case_id()
    await db.execute(
        "INSERT INTO cases (case_id, title, user_id) VALUES ($1, $2, $3)",
        case_id, title, user_id
    )

    # 2. ALSO write to Case Service
    try:
        await case_service_client.create_case({
            "case_id": case_id,
            "title": title,
            "user_id": user_id
        })
    except Exception as e:
        logger.error(f"Dual-write to case service failed: {e}")

    return case_id
```

**Step 6: Switch Reads to Case Service**

```python
# Monolith: Read from Case Service, fallback to local DB
async def get_case_monolith(case_id: str):
    try:
        # Try new service first
        return await case_service_client.get_case(case_id)
    except Exception as e:
        logger.warning(f"Case service unavailable, falling back to local DB: {e}")
        # Fallback to monolith DB
        return await db.fetch_one("SELECT * FROM cases WHERE case_id = $1", case_id)
```

**Step 7: Update Dependent Services**

```python
# Agent Service: Replace direct DB access with API calls
# OLD:
case = await db.fetch_one("SELECT * FROM cases WHERE case_id = $1", case_id)

# NEW:
case = await case_client.get_case(case_id)
```

**Step 8: Disable Monolith Case Routes**

```python
# Monolith: api/v1/routes/case.py
# DELETE or mark as deprecated
@router.post("/cases")
async def create_case_deprecated():
    raise HTTPException(410, "This endpoint has moved to /v1/cases on case service")
```

---

### 6.3 Rollback Procedures

**Rollback Triggers**:
- Error rate >5% for new service
- Latency p99 >2x baseline
- Data inconsistency detected
- Critical bug in new service

**Rollback Steps**:

```bash
# 1. Shift traffic back to monolith
kubectl patch ingress case-service -p '{"spec":{"rules":[{"http":{"paths":[{"backend":{"service":{"name":"faultmaven-monolith"}}}]}}]}}'

# 2. Disable new service
kubectl scale deployment fm-case-service --replicas=0

# 3. Monitor monolith performance
kubectl logs -f deployment/faultmaven-monolith --tail=100

# 4. Investigate issue in new service
kubectl logs deployment/fm-case-service --since=1h > case-service-errors.log

# 5. Fix and redeploy
# (fix code)
kubectl rollout restart deployment/fm-case-service
kubectl scale deployment fm-case-service --replicas=3

# 6. Gradual rollout again (10% → 50% → 100%)
```

---

## 7. Containerization Best Practices

### 7.1 Multi-Stage Dockerfile Template

**Objective**: Build secure, slim container images with minimal attack surface

**Production-Ready Dockerfile** (Python with Poetry):

```dockerfile
# fm-case-service/Dockerfile

# ============================================================================
# Stage 1: Builder - Install dependencies and build wheels
# ============================================================================
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN pip install --no-cache-dir poetry==1.7.1

# Copy dependency files
COPY pyproject.toml poetry.lock ./

# Export dependencies to requirements.txt (for wheel building)
RUN poetry export -f requirements.txt --output requirements.txt --without-hashes

# Build wheels for all dependencies
RUN pip wheel --no-cache-dir -r requirements.txt -w /wheels

# Copy source code
COPY src/ src/

# Build application wheel
RUN poetry build -f wheel && \
    cp dist/*.whl /wheels/

# ============================================================================
# Stage 2: Runtime - Distroless image with minimal dependencies
# ============================================================================
FROM gcr.io/distroless/python3-debian12:nonroot

WORKDIR /app

# Copy wheels from builder
COPY --from=builder /wheels /wheels

# Copy application code
COPY --from=builder /app/src /app/src

# Install all wheels (dependencies + application)
RUN python -m pip install --no-index --find-links=/wheels fm-case-service

# Use non-root user (distroless default: 65532)
USER 65532:65532

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PORT=8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]

# Expose port
EXPOSE 8000

# Run application
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Alternative: Standard Base Image** (if distroless doesn't work):

```dockerfile
# Stage 2: Runtime with standard slim image
FROM python:3.11-slim

WORKDIR /app

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -u 10001 appuser

# Copy wheels
COPY --from=builder /wheels /wheels

# Install wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels fm-case-service && \
    rm -rf /wheels

# Copy application
COPY src/ /app/src/

# Set ownership
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER 10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

### 7.2 12-Factor App Compliance

**Factor 1: Codebase** - One codebase per service
```bash
# Each service in its own repo
fm-auth-service/
fm-case-service/
fm-session-service/
```

**Factor 2: Dependencies** - Explicitly declare dependencies
```toml
# pyproject.toml
[tool.poetry.dependencies]
python = "^3.11"
fastapi = "0.109.0"
uvicorn = "0.27.0"
asyncpg = "0.29.0"
pydantic = "2.5.3"
```

**Factor 3: Config** - Store config in environment variables
```python
# src/config.py
from pydantic_settings import BaseSettings

class ServiceConfig(BaseSettings):
    """Configuration from environment variables"""
    database_url: str
    redis_url: str
    log_level: str = "INFO"
    jaeger_agent_host: str = "localhost"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

config = ServiceConfig()
```

**Factor 4: Backing Services** - Treat as attached resources
```yaml
# Kubernetes ConfigMap
apiVersion: v1
kind: ConfigMap
metadata:
  name: case-service-config
data:
  DATABASE_URL: "postgresql://case-db:5432/fm_case"
  REDIS_URL: "redis://fm-redis-cluster:6379"
```

**Factor 5: Build/Release/Run** - Strict separation
```bash
# Build: Create immutable artifact
docker build -t ghcr.io/faultmaven/fm-case-service:v1.2.3 .

# Release: Combine build with config
helm upgrade --install fm-case-service ./chart \
  --set image.tag=v1.2.3 \
  --set database.url="postgresql://..." \
  --namespace faultmaven-case

# Run: Execute release in environment
kubectl rollout status deployment/fm-case-service -n faultmaven-case
```

**Factor 6: Processes** - Execute as stateless processes
```python
# ❌ BAD: Storing state in memory
class CaseService:
    def __init__(self):
        self.cache = {}  # Lost on restart!

# ✅ GOOD: Stateless with external cache
class CaseService:
    def __init__(self, redis_client):
        self.cache = redis_client  # Survives restarts
```

**Factor 7: Port Binding** - Export services via port binding
```python
# main.py
import uvicorn
from fastapi import FastAPI

app = FastAPI()

if __name__ == "__main__":
    # Bind to PORT environment variable
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
```

**Factor 8: Concurrency** - Scale out via process model
```yaml
# Horizontal scaling with multiple replicas
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fm-case-service
spec:
  replicas: 3  # Scale horizontally
```

**Factor 9: Disposability** - Fast startup and graceful shutdown
```python
# Graceful shutdown handler
import signal
import sys

def graceful_shutdown(signum, frame):
    logger.info("Received shutdown signal, draining connections...")
    # Finish processing current requests
    server.should_exit = True
    # Close database connections
    db.close()
    sys.exit(0)

signal.signal(signal.SIGTERM, graceful_shutdown)
signal.signal(signal.SIGINT, graceful_shutdown)
```

**Factor 10: Dev/Prod Parity** - Keep environments similar
```bash
# Use same PostgreSQL version locally and in production
docker-compose.yml:
  postgres:
    image: postgres:15-alpine  # Same as production

# Use same environment variables
.env.example:
  DATABASE_URL=postgresql://localhost:5432/fm_case_dev
```

**Factor 11: Logs** - Treat logs as event streams
```python
# Structured JSON logging to stdout
import logging
import json

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_data = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "message": record.getMessage(),
            "service": "fm-case-service",
            "correlation_id": getattr(record, 'correlation_id', None)
        }
        return json.dumps(log_data)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logger.addHandler(handler)
```

**Factor 12: Admin Processes** - Run as one-off processes
```bash
# Database migrations as Kubernetes Job
apiVersion: batch/v1
kind: Job
metadata:
  name: case-service-migrate
spec:
  template:
    spec:
      containers:
      - name: migrate
        image: ghcr.io/faultmaven/fm-case-service:v1.2.3
        command: ["alembic", "upgrade", "head"]
      restartPolicy: OnFailure
```

---

### 7.3 Health & Readiness Probes

**Health Endpoint** (liveness probe):
```python
# src/routes/health.py
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

class HealthResponse(BaseModel):
    status: str
    service: str
    version: str

@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Liveness probe - is the service running?"""
    return HealthResponse(
        status="healthy",
        service="fm-case-service",
        version="1.2.3"
    )
```

**Readiness Endpoint** (readiness probe):
```python
@router.get("/ready")
async def readiness_check(db: Database = Depends(get_db)):
    """Readiness probe - can the service handle requests?"""
    # Check database connection
    try:
        await db.execute("SELECT 1")
    except Exception as e:
        raise HTTPException(503, f"Database unavailable: {e}")

    # Check Redis connection
    try:
        await redis.ping()
    except Exception as e:
        raise HTTPException(503, f"Redis unavailable: {e}")

    return {"status": "ready"}
```

**Kubernetes Probe Configuration**:
```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 30
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 3

readinessProbe:
  httpGet:
    path: /ready
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 5
  timeoutSeconds: 3
  failureThreshold: 2
```

---

### 7.4 Observability (Logs, Metrics, Traces)

**Distributed Tracing** (propagate traceparent):
```python
# Middleware to extract and propagate trace context
from opentelemetry import trace
from opentelemetry.propagate import extract

@app.middleware("http")
async def trace_propagation(request: Request, call_next):
    # Extract trace context from headers
    ctx = extract(request.headers)

    # Start span with parent context
    with trace.get_tracer(__name__).start_as_current_span(
        f"{request.method} {request.url.path}",
        context=ctx
    ) as span:
        # Add request metadata
        span.set_attribute("http.method", request.method)
        span.set_attribute("http.url", str(request.url))
        span.set_attribute("service.name", "fm-case-service")

        # Process request
        response = await call_next(request)

        # Add response metadata
        span.set_attribute("http.status_code", response.status_code)

        return response
```

**Prometheus Metrics**:
```python
from prometheus_client import Counter, Histogram, generate_latest

# Define metrics
http_requests_total = Counter(
    'http_requests_total',
    'Total HTTP requests',
    ['method', 'endpoint', 'status']
)

http_request_duration_seconds = Histogram(
    'http_request_duration_seconds',
    'HTTP request duration',
    ['method', 'endpoint']
)

@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start_time = time.time()

    response = await call_next(request)

    # Record metrics
    duration = time.time() - start_time
    http_requests_total.labels(
        method=request.method,
        endpoint=request.url.path,
        status=response.status_code
    ).inc()

    http_request_duration_seconds.labels(
        method=request.method,
        endpoint=request.url.path
    ).observe(duration)

    return response

@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint"""
    return Response(content=generate_latest(), media_type="text/plain")
```

**Structured JSON Logs**:
```python
import logging
import json
from contextvars import ContextVar

# Context variable for correlation ID
correlation_id_var: ContextVar[str] = ContextVar('correlation_id', default=None)

class StructuredLogger(logging.Formatter):
    def format(self, record):
        log_obj = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "service": "fm-case-service",
            "message": record.getMessage(),
            "correlation_id": correlation_id_var.get(),
            "module": record.module,
            "function": record.funcName
        }

        # Add exception info if present
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_obj)

# Configure logger
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(StructuredLogger())
logger.addHandler(handler)
```

---

### 7.5 Security Best Practices

**SBOM (Software Bill of Materials)**:
```bash
# Generate SBOM using Syft
syft packages ghcr.io/faultmaven/fm-case-service:v1.2.3 \
  -o spdx-json > sbom.json

# Store SBOM as artifact
gh release upload v1.2.3 sbom.json
```

**Image Signing** (Cosign):
```bash
# Sign container image
cosign sign --key cosign.key ghcr.io/faultmaven/fm-case-service:v1.2.3

# Verify signature before deployment
cosign verify --key cosign.pub ghcr.io/faultmaven/fm-case-service:v1.2.3
```

**CVE Scanning** (Trivy):
```bash
# Scan image for vulnerabilities
trivy image --severity HIGH,CRITICAL \
  ghcr.io/faultmaven/fm-case-service:v1.2.3

# Fail CI/CD if critical vulnerabilities found
trivy image --exit-code 1 --severity CRITICAL \
  ghcr.io/faultmaven/fm-case-service:v1.2.3
```

**Non-Root User**:
```dockerfile
# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -u 10001 appuser

# Set ownership
RUN chown -R appuser:appuser /app

# Switch to non-root
USER 10001
```

**Secrets Management**:
```yaml
# Kubernetes Secret for sensitive config
apiVersion: v1
kind: Secret
metadata:
  name: case-service-secrets
  namespace: faultmaven-case
type: Opaque
stringData:
  database-url: "postgresql://user:password@host:5432/db"
  jwt-secret: "super-secret-key"

---
# Reference in deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fm-case-service
spec:
  template:
    spec:
      containers:
      - name: case-service
        env:
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: case-service-secrets
              key: database-url
```

---

### 7.6 CI/CD Pipeline Template

**GitHub Actions Workflow** (`.github/workflows/ci.yml`):

```yaml
name: CI/CD Pipeline

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: |
          pip install black flake8 mypy
      - name: Run linters
        run: |
          black --check src/
          flake8 src/
          mypy src/

  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: |
          pip install poetry
          poetry install
      - name: Run tests
        run: |
          poetry run pytest --cov=src --cov-report=xml
      - name: Upload coverage
        uses: codecov/codecov-action@v3

  contract-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run consumer contract tests
        run: |
          pytest tests/contract/ --pact-broker-url=${{ secrets.PACT_BROKER_URL }}

  build:
    needs: [lint, test, contract-tests]
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to container registry
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Extract metadata
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}
          tags: |
            type=semver,pattern={{version}}
            type=semver,pattern={{major}}.{{minor}}
            type=sha,prefix={{branch}}-

      - name: Build and push image
        uses: docker/build-push-action@v5
        with:
          context: .
          push: ${{ github.event_name != 'pull_request' }}
          tags: ${{ steps.meta.outputs.tags }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

  scan:
    needs: [build]
    runs-on: ubuntu-latest
    steps:
      - name: Run Trivy vulnerability scanner
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:${{ github.sha }}
          format: 'sarif'
          output: 'trivy-results.sarif'
          severity: 'CRITICAL,HIGH'

      - name: Upload Trivy results to GitHub Security
        uses: github/codeql-action/upload-sarif@v2
        with:
          sarif_file: 'trivy-results.sarif'

  sbom:
    needs: [build]
    runs-on: ubuntu-latest
    steps:
      - name: Generate SBOM
        uses: anchore/sbom-action@v0
        with:
          image: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:${{ github.sha }}
          format: spdx-json
          output-file: sbom.spdx.json

      - name: Upload SBOM as artifact
        uses: actions/upload-artifact@v3
        with:
          name: sbom
          path: sbom.spdx.json

  deploy-dev:
    needs: [build, scan]
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'
    steps:
      - uses: actions/checkout@v4

      - name: Set up Kubernetes context
        uses: azure/k8s-set-context@v3
        with:
          method: kubeconfig
          kubeconfig: ${{ secrets.KUBE_CONFIG }}

      - name: Deploy to dev environment
        run: |
          helm upgrade --install fm-case-service ./chart \
            --namespace faultmaven-dev \
            --set image.tag=${{ github.sha }} \
            --set environment=dev \
            --wait
```

---

## 8. Phase-by-Phase Execution Plan

### Phase 0: Foundation & Dual-Track Setup (Week 1)

**Objective**: Establish dual-track codebase (PUBLIC monolithic + PRIVATE microservices) and define service boundaries

**IMPORTANT**: This phase creates TWO distinct versions from the current FaultMaven monolith:

1. **PUBLIC Track**: Open-source monolithic version (simplified, local, `http://localhost:8000`)
2. **PRIVATE Track**: Enterprise microservices version (8 services, K8s-ready, `https://app.faultmaven.ai`)

---

**Day 1-2: Feature Classification**

Create `FEATURE_CLASSIFICATION.md` documenting which code belongs to PUBLIC vs PRIVATE:

```markdown
# Feature Classification Matrix

## PUBLIC Features (open-source monolith)
- core/investigation/ (milestone-based engine)
- core/agent/ (LangGraph agent)
- tools/ (KB query, web search)
- infrastructure/llm/ (multi-provider routing)
- infrastructure/security/pii_redaction.py
- api/v1/routes/agent.py (Chat API) ✅ REQUIRED
- api/v1/routes/knowledge.py (Knowledge Base API) ✅ REQUIRED
- models/case.py (simplified version - no org_id)
- models/message.py
- models/evidence.py

## PRIVATE Features (enterprise microservices)
- api/v1/routes/organizations.py
- api/v1/routes/teams.py
- api/v1/routes/users.py (multi-user auth)
- models/organization.py
- models/team.py
- models/user.py (enterprise user model)
- infrastructure/persistence/postgres_repository.py
- infrastructure/persistence/redis_store.py
- infrastructure/persistence/chromadb_store.py
- services/domain/organization_service.py
- services/domain/team_service.py

## SHARED Features (different implementations)
- models/case.py → PUBLIC: simplified (single-user), PRIVATE: full (multi-tenant)
- persistence/ → PUBLIC: SQLite/in-memory, PRIVATE: PostgreSQL
- session/ → PUBLIC: in-memory dict, PRIVATE: Redis cluster
- knowledge/ → PUBLIC: simple vector store, PRIVATE: ChromaDB cluster
```

**Checklist**:
- [ ] Document all PUBLIC features (core investigation engine)
- [ ] Document all PRIVATE features (enterprise/multi-tenancy)
- [ ] Document all SHARED features (different implementations)
- [ ] Review with team for completeness

---

**Day 3: Public Repository Branch Creation**

In `/home/swhouse/projects/FaultMaven`:

```bash
# Create public branch
git checkout -b public-opensource

# Remove enterprise files
rm -rf faultmaven/api/v1/routes/organizations.py
rm -rf faultmaven/api/v1/routes/teams.py
rm -rf faultmaven/models/organization.py
rm -rf faultmaven/models/team.py
# ... remove all PRIVATE files from classification

# Create simplified implementations
mkdir -p faultmaven/infrastructure/persistence/inmemory/

# Files to create:
# - inmemory_case_repository.py (replace PostgreSQL)
# - inmemory_session_store.py (replace Redis)
# - inmemory_knowledge_store.py (replace ChromaDB)

# Update container.py to use in-memory dependencies
# Update environment.py to remove PostgreSQL/Redis/ChromaDB config

# Create pip installable setup
# File: setup.py with entry_points = {"console_scripts": ["faultmaven=faultmaven.cli:main"]}
# File: faultmaven/cli.py with click commands (serve, init, etc.)

# Update README.md for public version
cat > README.md << 'EOF'
# FaultMaven - AI-Powered Troubleshooting Copilot

Open-source AI assistant for technical troubleshooting.

## Installation

pip install faultmaven

## Usage

faultmaven serve --port 8000

## Access

- Chat API: http://localhost:8000/api/v1/chat
- Knowledge Base API: http://localhost:8000/api/v1/knowledge
- Interactive docs: http://localhost:8000/docs

## Features

✅ AI-powered troubleshooting conversations
✅ Knowledge base search and retrieval
✅ Multi-LLM support (OpenAI, Anthropic, Fireworks AI)
✅ PII redaction for privacy
✅ Local-first (no external database required)

## License

Apache 2.0
EOF

# Create LICENSE file (Apache 2.0)
curl -L https://www.apache.org/licenses/LICENSE-2.0.txt > LICENSE

git add .
git commit -m "feat: create public open-source monolithic version

- Remove enterprise features (organizations, teams, multi-user auth)
- Add in-memory storage implementations (SQLite/in-memory)
- Create CLI with 'faultmaven serve' command
- Include Chat API + Knowledge Base API
- Simplify to single-user local deployment
- Add Apache 2.0 license"
```

**Public Version Verification Checklist**:
- [ ] Chat API functional at `/api/v1/chat`
- [ ] Knowledge Base API functional at `/api/v1/knowledge`
- [ ] No PostgreSQL dependency (SQLite or in-memory only)
- [ ] No Redis dependency (in-memory dict)
- [ ] No ChromaDB dependency (simple vector store)
- [ ] No organization/team models
- [ ] Works with: `pip install -e .` → `faultmaven serve`
- [ ] Accessible at `http://localhost:8000`
- [ ] Apache 2.0 LICENSE file exists
- [ ] README.md updated for public use

---

**Day 4-5: Private Service Repositories Initialization**

Clone all 9 FaultMaven org repos and initialize structure:

```bash
cd /home/swhouse/projects/

# Clone all repos (already created via setup-org-repos.sh)
for repo in fm-auth-service fm-session-service fm-case-service fm-evidence-service \
            fm-investigation-service fm-knowledge-service fm-agent-service \
            fm-contracts fm-charts; do
  gh repo clone FaultMaven/$repo
done

# Initialize each service repo with standard structure
# Example for fm-case-service:
cd fm-case-service

mkdir -p src/case_service/{api,domain,infrastructure}
mkdir -p src/case_service/api/routes
mkdir -p src/case_service/domain/{models,services}
mkdir -p src/case_service/infrastructure/{persistence,events}
mkdir -p tests/{unit,integration,contract}

# Create pyproject.toml
cat > pyproject.toml << 'EOF'
[tool.poetry]
name = "fm-case-service"
version = "0.1.0"
description = "FaultMaven Case Management Service"

[tool.poetry.dependencies]
python = "^3.11"
fastapi = "^0.104.0"
uvicorn = {extras = ["standard"], version = "^0.24.0"}
asyncpg = "^0.29.0"
pydantic = "^2.4.0"
pydantic-settings = "^2.0.0"
redis = "^5.0.0"
httpx = "^0.25.0"
opentelemetry-api = "^1.20.0"
opentelemetry-sdk = "^1.20.0"
prometheus-client = "^0.18.0"

[tool.poetry.dev-dependencies]
pytest = "^7.4.0"
pytest-asyncio = "^0.21.0"
pytest-cov = "^4.1.0"
black = "^23.10.0"
flake8 = "^6.1.0"
mypy = "^1.6.0"
EOF

# Create Dockerfile (multi-stage)
cat > Dockerfile << 'EOF'
# Stage 1: Build
FROM python:3.11-slim as builder
WORKDIR /app
RUN pip install poetry
COPY pyproject.toml poetry.lock ./
RUN poetry export -f requirements.txt --output requirements.txt --without-hashes

# Stage 2: Runtime
FROM gcr.io/distroless/python3-debian12
COPY --from=builder /app/requirements.txt /app/
COPY src/ /app/src/
WORKDIR /app
CMD ["python", "-m", "src.case_service.main"]
EOF

# Create docker-compose.yml
cat > docker-compose.yml << 'EOF'
version: '3.8'
services:
  case-service:
    build: .
    ports:
      - "8001:8000"
    environment:
      - DATABASE_URL=postgresql://postgres:password@postgres:5432/fm_case
      - REDIS_URL=redis://redis:6379
    depends_on:
      - postgres
      - redis

  postgres:
    image: postgres:15-alpine
    environment:
      POSTGRES_DB: fm_case
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: password
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data

volumes:
  postgres_data:
  redis_data:
EOF

# Create .env.example
cat > .env.example << 'EOF'
DATABASE_URL=postgresql://postgres:password@localhost:5432/fm_case
REDIS_URL=redis://localhost:6379
LOG_LEVEL=INFO
ENVIRONMENT=development
EOF

# Create README.md
cat > README.md << 'EOF'
# FM Case Service

FaultMaven microservice for case management.

## Local Development

docker-compose up --build

## Testing

pytest tests/ --cov=src
EOF

git add .
git commit -m "chore: initialize service structure"
git push origin main
```

**Repeat for all 8 services**: auth, session, case, evidence, investigation, knowledge, agent, analytics

**Checklist**:
- [ ] All 8 service repos cloned
- [ ] Standard structure created (src/, tests/, Dockerfile, docker-compose.yml)
- [ ] pyproject.toml with dependencies
- [ ] Multi-stage Dockerfile (builder + distroless)
- [ ] docker-compose.yml with service + PostgreSQL + Redis
- [ ] .env.example with configuration
- [ ] README.md with setup instructions

---

**Day 6: Service Boundary Mapping**

Create `SERVICE_EXTRACTION_MAP.md` in each service repo documenting extraction plan:

**Example: fm-case-service/SERVICE_EXTRACTION_MAP.md**

```markdown
# Case Service Extraction Map

## Source Files (from FaultMaven monolith)

| Monolith File | Destination | Action |
|---------------|-------------|--------|
| faultmaven/models/case.py | src/case_service/domain/models/case.py | Extract + enhance (add org_id, team_id) |
| faultmaven/services/domain/case_service.py | src/case_service/domain/services/case_service.py | Extract business logic |
| faultmaven/api/v1/routes/cases.py | src/case_service/api/routes/cases.py | Extract API endpoints |
| faultmaven/infrastructure/persistence/case_repository.py | src/case_service/infrastructure/persistence/repository.py | Extract data access |

## Database Tables (exclusive ownership)

| Table Name | Source Schema | Action |
|------------|---------------|--------|
| cases | 001_initial_hybrid_schema.sql | MIGRATE to fm_case database |
| case_messages | 001_initial_hybrid_schema.sql | MIGRATE to fm_case database |
| case_status_transitions | 001_initial_hybrid_schema.sql | MIGRATE to fm_case database |
| case_tags | 001_initial_hybrid_schema.sql | MIGRATE to fm_case database |

## Events Published

| Event Name | AsyncAPI Schema | Trigger |
|------------|-----------------|---------|
| case.created.v1 | contracts/asyncapi/case-events.yaml | POST /cases |
| case.updated.v1 | contracts/asyncapi/case-events.yaml | PUT /cases/{id} |
| case.status_changed.v1 | contracts/asyncapi/case-events.yaml | POST /cases/{id}/status |
| case.closed.v1 | contracts/asyncapi/case-events.yaml | POST /cases/{id}/close |

## Events Consumed

| Event Name | Source Service | Action |
|------------|----------------|--------|
| evidence.uploaded.v1 | Evidence Service | Link evidence to case |
| hypothesis.validated.v1 | Investigation Service | Update case status |

## API Dependencies

| Dependency | Purpose | Fallback Strategy |
|------------|---------|-------------------|
| Auth Service | Validate user tokens | Circuit breaker (deny if down) |
| Session Service | Get active session | Circuit breaker (return 503) |

## Migration Checklist

- [ ] Extract domain models (Case, CaseMessage, CaseStatusTransition)
- [ ] Extract business logic (CaseService with validation rules)
- [ ] Extract API routes (CRUD + status transitions)
- [ ] Extract repository (PostgreSQL data access)
- [ ] Create database migration scripts (001_initial_schema.sql)
- [ ] Implement event publishing (outbox pattern)
- [ ] Implement event consumption (inbox pattern)
- [ ] Add circuit breakers for auth/session dependencies
- [ ] Write unit tests (80%+ coverage)
- [ ] Write integration tests (DB + events)
- [ ] Write contract tests (provider verification)
```

**Repeat for all 8 services**

**Checklist**:
- [ ] SERVICE_EXTRACTION_MAP.md in all 8 service repos
- [ ] Source file mapping complete
- [ ] Database table ownership documented
- [ ] Events published/consumed documented
- [ ] API dependencies documented

---

**Day 7: Contract Definitions**

In `fm-contracts` repo, create OpenAPI and AsyncAPI specifications:

```bash
cd /home/swhouse/projects/fm-contracts

mkdir -p contracts/openapi
mkdir -p contracts/asyncapi
mkdir -p contracts/schemas/common

# Create OpenAPI spec for Case Service
cat > contracts/openapi/case-service.yaml << 'EOF'
openapi: 3.1.0
info:
  title: Case Service API
  version: 1.0.0
  description: FaultMaven case management microservice

servers:
  - url: https://app.faultmaven.ai/api/v1
    description: Production
  - url: http://localhost:8001/api/v1
    description: Local development

paths:
  /cases:
    post:
      summary: Create a new case
      operationId: createCase
      tags: [Cases]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateCaseRequest'
      responses:
        '201':
          description: Case created successfully
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CaseResponse'
        '400':
          $ref: '../schemas/common/error.yaml#/components/responses/BadRequest'
        '401':
          $ref: '../schemas/common/error.yaml#/components/responses/Unauthorized'

    get:
      summary: List cases for user
      operationId: listCases
      tags: [Cases]
      parameters:
        - name: user_id
          in: query
          required: true
          schema:
            type: string
        - name: status
          in: query
          schema:
            type: string
            enum: [consulting, problem_verification, in_progress, resolved, closed]
        - $ref: '../schemas/common/pagination.yaml#/components/parameters/Page'
        - $ref: '../schemas/common/pagination.yaml#/components/parameters/PageSize'
      responses:
        '200':
          description: List of cases
          content:
            application/json:
              schema:
                type: object
                properties:
                  cases:
                    type: array
                    items:
                      $ref: '#/components/schemas/CaseResponse'
                  pagination:
                    $ref: '../schemas/common/pagination.yaml#/components/schemas/PaginationMetadata'

components:
  schemas:
    CreateCaseRequest:
      type: object
      required: [user_id, title, initial_description]
      properties:
        user_id:
          type: string
          format: uuid
        title:
          type: string
          minLength: 1
          maxLength: 200
        initial_description:
          type: string
          minLength: 10
        org_id:
          type: string
          format: uuid
        team_id:
          type: string
          format: uuid

    CaseResponse:
      type: object
      properties:
        case_id:
          type: string
          format: uuid
        user_id:
          type: string
          format: uuid
        org_id:
          type: string
          format: uuid
        title:
          type: string
        status:
          type: string
          enum: [consulting, problem_verification, investigating, root_cause_analysis, solution_planning, solution_validation, resolved, closed]
        created_at:
          type: string
          format: date-time
        updated_at:
          type: string
          format: date-time
EOF

# Create AsyncAPI spec for Case Events
cat > contracts/asyncapi/case-events.yaml << 'EOF'
asyncapi: 2.6.0
info:
  title: Case Service Events
  version: 1.0.0
  description: Events published by the Case Service

channels:
  case.created.v1:
    description: Fired when a new case is created
    publish:
      message:
        $ref: '#/components/messages/CaseCreated'

  case.updated.v1:
    description: Fired when case details are updated
    publish:
      message:
        $ref: '#/components/messages/CaseUpdated'

  case.status_changed.v1:
    description: Fired when case status transitions
    publish:
      message:
        $ref: '#/components/messages/CaseStatusChanged'

components:
  messages:
    CaseCreated:
      name: CaseCreated
      contentType: application/json
      payload:
        type: object
        required: [event_id, event_type, aggregate_id, aggregate_type, case_id, user_id, title, created_at]
        properties:
          event_id:
            type: string
            format: uuid
          event_type:
            type: string
            const: case.created.v1
          aggregate_id:
            type: string
            format: uuid
          aggregate_type:
            type: string
            const: case
          case_id:
            type: string
            format: uuid
          user_id:
            type: string
            format: uuid
          org_id:
            type: string
            format: uuid
          title:
            type: string
          status:
            type: string
          created_at:
            type: string
            format: date-time

    CaseUpdated:
      name: CaseUpdated
      contentType: application/json
      payload:
        type: object
        required: [event_id, event_type, case_id, updated_at, changes]
        properties:
          event_id:
            type: string
            format: uuid
          event_type:
            type: string
            const: case.updated.v1
          case_id:
            type: string
            format: uuid
          updated_at:
            type: string
            format: date-time
          changes:
            type: object
            description: Fields that were changed

    CaseStatusChanged:
      name: CaseStatusChanged
      contentType: application/json
      payload:
        type: object
        required: [event_id, case_id, old_status, new_status, changed_at]
        properties:
          event_id:
            type: string
            format: uuid
          event_type:
            type: string
            const: case.status_changed.v1
          case_id:
            type: string
            format: uuid
          old_status:
            type: string
          new_status:
            type: string
          changed_at:
            type: string
            format: date-time
EOF

git add .
git commit -m "feat: add OpenAPI and AsyncAPI contracts for Case Service"
git push origin main
```

**Create contracts for all 7 API services** (auth, session, case, evidence, investigation, knowledge, agent)

**Checklist**:
- [ ] OpenAPI 3.1 specs for all 7 services
- [ ] AsyncAPI 2.6 specs for all event-driven services
- [ ] Common error schemas (error.yaml)
- [ ] Common pagination schemas (pagination.yaml)
- [ ] All contracts committed to fm-contracts repo

---

**Day 7: Comparison Matrix Documentation**

Create `PUBLIC_VS_PRIVATE_COMPARISON.md` in FaultMaven root:

```markdown
# Public vs Private Version Comparison

| Feature | PUBLIC (Open-Source) | PRIVATE (Enterprise) |
|---------|---------------------|---------------------|
| **Chat API** | ✅ Included (`/api/v1/chat`) | ✅ Included (`/api/v1/chat`) |
| **Knowledge Base API** | ✅ Included (`/api/v1/knowledge`) | ✅ Included (`/api/v1/knowledge`) |
| **Access URL** | `http://localhost:8000` (local) | `https://app.faultmaven.ai` (production) |
| **Installation** | `pip install faultmaven` | Docker Compose / Kubernetes |
| **Architecture** | Monolithic (single process) | 8 Microservices (distributed) |
| **Database** | SQLite / In-Memory | PostgreSQL (per service) |
| **Session Storage** | In-Memory (Python dict) | Redis Cluster (distributed) |
| **Vector Store** | Simple in-memory | ChromaDB Cluster |
| **Multi-Tenancy** | ❌ Single user only | ✅ Organizations + Teams |
| **Authentication** | ❌ None / Simple token | ✅ OAuth2 + SSO + RBAC |
| **Authorization** | ❌ N/A | ✅ Role-based access control |
| **User Management** | ❌ Single user | ✅ Multi-user with invitations |
| **Collaboration** | ❌ None | ✅ Team case sharing |
| **Audit Logging** | ❌ Basic logs | ✅ Full audit trail |
| **Scalability** | Vertical (single machine) | Horizontal (K8s auto-scaling) |
| **High Availability** | ❌ Single point of failure | ✅ Multi-replica services |
| **Deployment** | Local (`faultmaven serve`) | Kubernetes with Helm charts |
| **License** | Apache 2.0 (open-source) | Proprietary (enterprise) |
| **Target Users** | Individual developers, hobbyists | Enterprise teams, SaaS customers |
| **Pricing** | Free | Subscription-based |
| **Support** | Community (GitHub issues) | Commercial support SLA |
| **Compliance** | N/A | SOC2, GDPR, HIPAA ready |

## Codebase Structure

### PUBLIC Version (`public-opensource` branch)
```
FaultMaven/
├── faultmaven/
│   ├── core/                   # Investigation engine
│   ├── tools/                  # AI agent tools
│   ├── infrastructure/
│   │   ├── llm/               # LLM routing
│   │   └── persistence/
│   │       └── inmemory/      # SQLite/in-memory stores
│   ├── api/v1/routes/
│   │   ├── agent.py           # Chat API ✅
│   │   └── knowledge.py       # KB API ✅
│   └── cli.py                 # CLI entry point
├── setup.py                    # Pip package
├── LICENSE                     # Apache 2.0
└── README.md                   # User guide
```

### PRIVATE Version (8 service repos)
```
fm-auth-service/              # User authentication + RBAC
fm-session-service/           # Session management (Redis)
fm-case-service/              # Case CRUD + workflow
fm-evidence-service/          # Evidence storage
fm-investigation-service/     # Investigation orchestration
fm-knowledge-service/         # Vector search (ChromaDB)
fm-agent-service/             # AI agent orchestration
fm-analytics-service/         # Usage analytics + metrics
fm-contracts/                 # OpenAPI + AsyncAPI specs
fm-charts/                    # Helm charts for K8s deployment
```

## Migration Strategy

1. **Phase 0 (Week 1)**: Create both versions simultaneously
   - PUBLIC: Strip enterprise features, add in-memory storage
   - PRIVATE: Initialize 8 service repos with contracts

2. **Phase 1-6 (Week 2-12)**: Extract PRIVATE services incrementally
   - Week 2-3: Auth Service
   - Week 4-5: Session + Case Services
   - Week 6-8: Evidence + Investigation Services
   - Week 9-10: Knowledge + Agent Services
   - Week 11-12: Analytics + final cleanup

3. **Parallel Maintenance**: Both versions maintained going forward
   - PUBLIC: Security fixes + core feature improvements
   - PRIVATE: Enterprise features + scalability enhancements
```

**Checklist**:
- [ ] PUBLIC_VS_PRIVATE_COMPARISON.md created
- [ ] All feature differences documented
- [ ] Codebase structure comparison included
- [ ] Migration strategy timeline included

---

### Phase 0 Deliverables Checklist

**PUBLIC Track**:
- [ ] `public-opensource` branch created
- [ ] All enterprise features removed (organizations, teams, multi-user auth)
- [ ] In-memory storage implementations created (SQLite, dict, simple vector)
- [ ] Chat API functional at `/api/v1/chat`
- [ ] Knowledge Base API functional at `/api/v1/knowledge`
- [ ] `pip install -e .` works
- [ ] `faultmaven serve` command works (accessible at `http://localhost:8000`)
- [ ] Apache 2.0 LICENSE file exists
- [ ] README.md updated for public use
- [ ] No PostgreSQL dependency
- [ ] No Redis dependency
- [ ] No ChromaDB dependency

**PRIVATE Track**:
- [ ] All 9 repos cloned and initialized (auth, session, case, evidence, investigation, knowledge, agent, analytics, contracts, charts)
- [ ] Standard service structure in each repo (src/, tests/, Dockerfile, docker-compose.yml)
- [ ] SERVICE_EXTRACTION_MAP.md in each service repo
- [ ] OpenAPI 3.1 contracts for all 7 API services (in fm-contracts)
- [ ] AsyncAPI 2.6 event schemas for all event-driven services (in fm-contracts)
- [ ] Dockerfile (multi-stage, distroless) in each service
- [ ] docker-compose.yml (service + PostgreSQL + Redis) in each service
- [ ] pyproject.toml with dependencies in each service
- [ ] README.md with setup instructions in each service

**Documentation**:
- [ ] FEATURE_CLASSIFICATION.md complete (PUBLIC vs PRIVATE vs SHARED)
- [ ] PUBLIC_VS_PRIVATE_COMPARISON.md complete (feature matrix)

**Verification Before Phase 1**:
- [ ] PUBLIC version runs locally: `cd FaultMaven && git checkout public-opensource && pip install -e . && faultmaven serve`
- [ ] PUBLIC version accessible at: `http://localhost:8000/docs`
- [ ] PRIVATE service repos all have valid structure and can build: `docker-compose build`
- [ ] All contracts validated with tools: `npx @stoplight/spectral-cli lint contracts/openapi/*.yaml`

---

**Notes**:
- Phase 0 focuses on CODE ORGANIZATION, not K8s deployment
- Services will be built and tested locally with Docker Compose
- K8s deployment comes later (Phase 7+)
- Both tracks evolve in parallel throughout migration

---

### Phase 1: Extract Auth Service (Week 2-3)

**Objective**: First microservice extraction, establish patterns

**Week 2 Tasks**:

**Day 8-9: Auth Service Implementation**
- [ ] Create FastAPI application in `fm-auth-service`
- [ ] Implement auth endpoints (login, logout, register, user management)
- [ ] Create PostgreSQL migrations for auth tables
- [ ] Implement JWT token generation (RS256)
- [ ] Add Redis for token blacklist
- [ ] Write unit tests (target: 80% coverage)

**Day 10-11: Database Migration**
- [ ] Create new `fm_auth` PostgreSQL database
- [ ] Run migration script to copy users, organizations, teams from monolith
- [ ] Verify data integrity (row counts, sample checks)
- [ ] Setup database read replica for auth service

**Day 12-13: Dual-Write Implementation**
- [ ] Add dual-write logic to monolith auth endpoints
- [ ] Deploy dual-write version to staging
- [ ] Monitor data consistency between monolith and auth service
- [ ] Run data validation scripts hourly

**Day 14: Traffic Shift Preparation**
- [ ] Deploy auth service to production (0% traffic)
- [ ] Configure API Gateway with traffic splitting
- [ ] Setup monitoring dashboards (latency, error rate, throughput)
- [ ] Create runbook for rollback

**Week 3 Tasks**:

**Day 15: 10% Traffic Shift**
- [ ] Route 10% of `/auth/*` traffic to new service
- [ ] Monitor error rates (target: <1% errors)
- [ ] Monitor latency (target: p99 <200ms)
- [ ] Check data consistency

**Day 16: Monitor and Fix**
- [ ] Analyze logs for issues
- [ ] Fix bugs found in 10% traffic
- [ ] Deploy hotfixes if needed

**Day 17: 50% Traffic Shift**
- [ ] Route 50% traffic to new service
- [ ] Continue monitoring
- [ ] Perform load testing

**Day 18-19: 100% Traffic Shift**
- [ ] Route 100% traffic to auth service
- [ ] Disable dual-write in monolith
- [ ] Mark monolith auth routes as deprecated

**Day 20-21: Cleanup**
- [ ] Remove monolith auth code
- [ ] Drop auth tables from monolith database (after backup)
- [ ] Update documentation

**Deliverables**:
- ✅ Auth service handling 100% authentication traffic
- ✅ Monolith auth code removed
- ✅ SLO met: 99.9% uptime, <100ms p99 latency

---

### Phase 2: Extract Session Service (Week 4-5)

**Objective**: Extract Redis-based session management

**Week 4 Tasks**:

**Day 22-23: Session Service Implementation**
- [ ] Create FastAPI application in `fm-session-service`
- [ ] Implement session endpoints (create, get, delete, heartbeat)
- [ ] Connect to existing Redis cluster
- [ ] Implement session TTL management
- [ ] Add rate limiting per user
- [ ] Write unit and integration tests

**Day 24-25: Deploy and Test**
- [ ] Deploy to staging
- [ ] Run integration tests with auth service
- [ ] Load testing (target: 10,000 req/sec)
- [ ] Deploy to production (0% traffic)

**Day 26: Traffic Shift**
- [ ] Route `/sessions/*` traffic to new service
- [ ] Monitor performance
- [ ] No dual-write needed (Redis is shared)

**Day 27-28: Cleanup**
- [ ] Remove session code from monolith
- [ ] Update monolith to call session service API

**Deliverables**:
- ✅ Session service operational
- ✅ SLO met: 99.9% uptime, <50ms p99 latency

---

### Phase 3: Extract Case Service (Week 6-8)

**Objective**: Extract core case management (most complex migration)

**Week 6 Tasks**:

**Day 29-31: Case Service Implementation**
- [ ] Create FastAPI application in `fm-case-service`
- [ ] Implement case CRUD endpoints
- [ ] Implement case message endpoints
- [ ] Implement case status management
- [ ] Create PostgreSQL migrations (cases, case_messages, case_status_transitions, case_tags)
- [ ] Implement event publishing (case.created, case.updated, etc.)

**Day 32-33: Database Migration**
- [ ] Create `fm_case` database
- [ ] Migrate case data from monolith (millions of rows)
- [ ] Verify data integrity
- [ ] Setup read replicas

**Day 34-35: Dual-Write Period**
- [ ] Implement dual-write in monolith
- [ ] Deploy to staging
- [ ] Monitor data consistency
- [ ] Fix any issues

**Week 7 Tasks**:

**Day 36-37: Update Dependent Services**
- [ ] Update Agent Service to call Case Service API
- [ ] Update Analytics Service to call Case Service API
- [ ] Update Investigation Service to call Case Service API
- [ ] Deploy updated services to staging

**Day 38-39: Traffic Shift (10% → 50%)**
- [ ] Route 10% traffic to case service
- [ ] Monitor and fix issues
- [ ] Route 50% traffic

**Week 8 Tasks**:

**Day 40-42: Traffic Shift (100%)**
- [ ] Route 100% traffic to case service
- [ ] Disable dual-write
- [ ] Monitor for 3 days

**Day 43-44: Cleanup**
- [ ] Remove monolith case code
- [ ] Drop case tables from monolith DB
- [ ] Update documentation

**Deliverables**:
- ✅ Case service operational
- ✅ All case data migrated
- ✅ SLO met: 99.5% uptime, <500ms p99 latency

---

### Phase 4: Extract Knowledge Service (Week 9)

**Objective**: Extract vector search and KB management

**Similar to Case Service but with ChromaDB instead of PostgreSQL**

**Deliverables**:
- ✅ Knowledge service operational
- ✅ 3 ChromaDB collections managed
- ✅ SLO met: 99% uptime, <2s p99 latency

---

### Phase 5: Extract Agent Orchestrator (Week 10)

**Objective**: Extract investigation orchestration (stateless)

**Easier than previous services** (no database, purely orchestration)

**Deliverables**:
- ✅ Agent service coordinating investigations
- ✅ LLM routing operational
- ✅ SLO met: 95% uptime, <10s p99 latency

---

### Phase 6: Extract Remaining Services (Week 11)

**Objective**: Complete microservices migration

**Services**:
- Evidence Service (owns evidence, uploaded_files, agent_tool_calls)
- Investigation Service (owns hypotheses, solutions)
- Analytics Service (owns usage metrics)

**Deliverables**:
- ✅ All 8 core services operational
- ✅ Monolith decommissioned

---

### Phase 7: Optimization & Hardening (Week 12)

**Objective**: Production readiness and optimization

**Tasks**:
- [ ] Load testing all services
- [ ] Chaos engineering (kill pods, simulate failures)
- [ ] Security audit (pen testing, OWASP checks)
- [ ] Performance optimization (caching, query tuning)
- [ ] Documentation update
- [ ] Runbook creation

**Deliverables**:
- ✅ All services meet SLOs under load
- ✅ Failure scenarios tested and documented
- ✅ Security audit passed
- ✅ Runbooks complete

---

## 8. Kubernetes Deployment Architecture

### 8.1 Cluster Topology

**Kubernetes Cluster** (faultmaven-k8s-infra):

```
Cluster: faultmaven-production
- 7 nodes total
  - 3 master nodes (HA control plane with kube-vip)
  - 2 data worker nodes (PostgreSQL, Redis, ChromaDB)
  - 2 app worker nodes (microservices)

Node Labels:
  node-role.kubernetes.io/master=true
  node-type=data        # Data worker nodes
  node-type=app         # App worker nodes
  storage-tier=fast     # RAID10 storage nodes
  storage-tier=standard # RAID5 storage nodes
```

**Namespaces**:
```
faultmaven-auth         # Auth service and auth database
faultmaven-case         # Case service and case database
faultmaven-evidence     # Evidence service
faultmaven-investigation # Investigation service
faultmaven-session      # Session service and Redis
faultmaven-knowledge    # Knowledge service and ChromaDB
faultmaven-agent        # Agent orchestrator
faultmaven-analytics    # Analytics service
faultmaven-ingress      # API Gateway and ingress
faultmaven-observability # Grafana, Prometheus, Jaeger
faultmaven-data         # PostgreSQL clusters, Redis, ChromaDB
```

### 8.2 Service Deployment Example

**Auth Service Helm Chart** (`fm-charts/auth-service/`):

```yaml
# values.yaml
replicaCount: 3

image:
  repository: ghcr.io/faultmaven/fm-auth-service
  tag: "1.0.0"
  pullPolicy: IfNotPresent

service:
  type: ClusterIP
  port: 8000

resources:
  requests:
    cpu: 200m
    memory: 512Mi
  limits:
    cpu: 1000m
    memory: 2Gi

autoscaling:
  enabled: true
  minReplicas: 3
  maxReplicas: 10
  targetCPUUtilizationPercentage: 70
  targetMemoryUtilizationPercentage: 80

env:
  - name: DATABASE_URL
    valueFrom:
      secretKeyRef:
        name: auth-db-secret
        key: connection-url
  - name: REDIS_URL
    value: "redis://fm-redis-cluster:6379"
  - name: JWT_SECRET
    valueFrom:
      secretKeyRef:
        name: auth-jwt-secret
        key: private-key

livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 30
  periodSeconds: 10

readinessProbe:
  httpGet:
    path: /ready
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 5

podDisruptionBudget:
  minAvailable: 2

affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
      - matchExpressions:
        - key: node-type
          operator: In
          values:
          - app
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      podAffinityTerm:
        labelSelector:
          matchExpressions:
          - key: app
            operator: In
            values:
            - auth-service
        topologyKey: kubernetes.io/hostname
```

**Deployment Manifest** (generated from Helm):

```yaml
# templates/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fm-auth-service
  namespace: faultmaven-auth
  labels:
    app: auth-service
    version: "1.0.0"
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  selector:
    matchLabels:
      app: auth-service
  template:
    metadata:
      labels:
        app: auth-service
        version: "1.0.0"
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8000"
        prometheus.io/path: "/metrics"
    spec:
      serviceAccountName: fm-auth-service
      containers:
      - name: auth-service
        image: ghcr.io/faultmaven/fm-auth-service:1.0.0
        ports:
        - containerPort: 8000
          name: http
        env:
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: auth-db-secret
              key: connection-url
        - name: LOG_LEVEL
          value: "INFO"
        - name: TRACING_ENABLED
          value: "true"
        - name: JAEGER_AGENT_HOST
          value: "jaeger-agent.faultmaven-observability.svc.cluster.local"
        resources:
          requests:
            cpu: 200m
            memory: 512Mi
          limits:
            cpu: 1000m
            memory: 2Gi
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /ready
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 5
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: node-type
                operator: In
                values:
                - app
```

### 8.3 Database Deployment

**PostgreSQL for Auth Service** (using Zalando Postgres Operator):

```yaml
# auth-db-cluster.yaml
apiVersion: acid.zalan.do/v1
kind: postgresql
metadata:
  name: fm-auth-db
  namespace: faultmaven-data
spec:
  teamId: "faultmaven"
  numberOfInstances: 3
  users:
    fm_auth:
    - superuser
    - createdb
  databases:
    fm_auth: fm_auth
  postgresql:
    version: "15"
  volume:
    size: 100Gi
    storageClass: longhorn-fast  # RAID10 for database
  resources:
    requests:
      cpu: 2000m
      memory: 4Gi
    limits:
      cpu: 4000m
      memory: 8Gi
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
      - matchExpressions:
        - key: node-type
          operator: In
          values:
          - data
  patroni:
    pg_hba:
    - hostssl all all 0.0.0.0/0 md5
    - host all all 0.0.0.0/0 md5
```

**Redis Cluster** (using Bitnami Helm Chart):

```yaml
# redis-cluster.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: redis-cluster-config
  namespace: faultmaven-data
data:
  redis.conf: |
    maxmemory 8gb
    maxmemory-policy allkeys-lru
    save ""
    appendonly yes

---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis-cluster
  namespace: faultmaven-data
spec:
  serviceName: redis-cluster
  replicas: 6
  selector:
    matchLabels:
      app: redis-cluster
  template:
    metadata:
      labels:
        app: redis-cluster
    spec:
      containers:
      - name: redis
        image: redis:7.2-alpine
        ports:
        - containerPort: 6379
          name: client
        - containerPort: 16379
          name: gossip
        command:
        - redis-server
        - /conf/redis.conf
        - --cluster-enabled yes
        - --cluster-config-file /data/nodes.conf
        - --cluster-node-timeout 5000
        volumeMounts:
        - name: conf
          mountPath: /conf
        - name: data
          mountPath: /data
        resources:
          requests:
            cpu: 500m
            memory: 2Gi
          limits:
            cpu: 1000m
            memory: 8Gi
      volumes:
      - name: conf
        configMap:
          name: redis-cluster-config
  volumeClaimTemplates:
  - metadata:
      name: data
    spec:
      accessModes: ["ReadWriteOnce"]
      storageClassName: longhorn-fast
      resources:
        requests:
          storage: 50Gi
```

### 8.4 Ingress Configuration

**NGINX Ingress Controller**:

```yaml
# ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: faultmaven-api
  namespace: faultmaven-ingress
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
    nginx.ingress.kubernetes.io/rate-limit: "100"
    nginx.ingress.kubernetes.io/cors-enable: "true"
    nginx.ingress.kubernetes.io/enable-cors: "true"
    nginx.ingress.kubernetes.io/cors-allow-origin: "https://app.faultmaven.com"
    nginx.ingress.kubernetes.io/configuration-snippet: |
      more_set_headers "X-Correlation-ID: $request_id";
spec:
  ingressClassName: nginx
  tls:
  - hosts:
    - api.faultmaven.com
    secretName: faultmaven-api-tls
  rules:
  - host: api.faultmaven.com
    http:
      paths:
      - path: /auth
        pathType: Prefix
        backend:
          service:
            name: fm-auth-service
            port:
              number: 8000
      - path: /cases
        pathType: Prefix
        backend:
          service:
            name: fm-case-service
            port:
              number: 8000
      - path: /sessions
        pathType: Prefix
        backend:
          service:
            name: fm-session-service
            port:
              number: 8000
      - path: /kb
        pathType: Prefix
        backend:
          service:
            name: fm-knowledge-service
            port:
              number: 8000
      - path: /investigate
        pathType: Prefix
        backend:
          service:
            name: fm-agent-service
            port:
              number: 8000
```

---

## 9. Operational Readiness

### 9.1 Service Level Objectives (SLOs)

**Tier 1: Critical Services (99.9% uptime)**
- Auth Service: 99.9% availability, <100ms p99 latency
- Session Service: 99.9% availability, <50ms p99 latency

**Tier 2: Core Services (99.5% uptime)**
- Case Service: 99.5% availability, <500ms p99 latency
- Evidence Service: 99.5% availability, <1s p99 latency

**Tier 3: Supporting Services (99.0% uptime)**
- Knowledge Service: 99.0% availability, <2s p99 latency
- Investigation Service: 99.0% availability, <1s p99 latency

**Tier 4: Best-Effort Services (95% uptime)**
- Agent Orchestrator: 95% availability, <10s p99 latency (LLM dependency)
- Analytics Service: 95% availability, <3s p99 latency

**Error Budget**:
```
99.9% SLO = 43 minutes downtime per month
99.5% SLO = 3.6 hours downtime per month
99.0% SLO = 7.2 hours downtime per month
95.0% SLO = 36 hours downtime per month
```

### 9.2 Monitoring & Alerting

**Prometheus Metrics**:

```yaml
# ServiceMonitor for Auth Service
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: fm-auth-service
  namespace: faultmaven-auth
spec:
  selector:
    matchLabels:
      app: auth-service
  endpoints:
  - port: http
    path: /metrics
    interval: 15s
```

**Grafana Dashboards**:

```json
{
  "dashboard": {
    "title": "FaultMaven Auth Service",
    "panels": [
      {
        "title": "Request Rate",
        "targets": [{
          "expr": "sum(rate(http_requests_total{service='auth-service'}[5m]))"
        }]
      },
      {
        "title": "P99 Latency",
        "targets": [{
          "expr": "histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{service='auth-service'}[5m])) by (le))"
        }]
      },
      {
        "title": "Error Rate",
        "targets": [{
          "expr": "sum(rate(http_requests_total{service='auth-service',status=~'5..'}[5m])) / sum(rate(http_requests_total{service='auth-service'}[5m]))"
        }]
      },
      {
        "title": "SLO Compliance",
        "targets": [{
          "expr": "(1 - (sum(rate(http_requests_total{service='auth-service',status=~'5..'}[30d])) / sum(rate(http_requests_total{service='auth-service'}[30d])))) * 100"
        }]
      }
    ]
  }
}
```

**Alerting Rules**:

```yaml
# prometheus-rules.yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: faultmaven-alerts
  namespace: faultmaven-observability
spec:
  groups:
  - name: auth-service
    interval: 30s
    rules:
    - alert: AuthServiceHighErrorRate
      expr: |
        (sum(rate(http_requests_total{service="auth-service",status=~"5.."}[5m])) /
         sum(rate(http_requests_total{service="auth-service"}[5m]))) > 0.05
      for: 5m
      labels:
        severity: critical
        service: auth-service
      annotations:
        summary: "Auth service error rate above 5%"
        description: "Auth service has {{ $value }}% error rate"

    - alert: AuthServiceHighLatency
      expr: |
        histogram_quantile(0.99,
          sum(rate(http_request_duration_seconds_bucket{service="auth-service"}[5m])) by (le)
        ) > 0.1
      for: 10m
      labels:
        severity: warning
        service: auth-service
      annotations:
        summary: "Auth service p99 latency above 100ms"

    - alert: AuthServiceSLOBreach
      expr: |
        (1 - (sum(rate(http_requests_total{service="auth-service",status=~"5.."}[30d])) /
              sum(rate(http_requests_total{service="auth-service"}[30d])))) < 0.999
      for: 1h
      labels:
        severity: critical
        service: auth-service
      annotations:
        summary: "Auth service SLO breached (99.9%)"
        description: "Current availability: {{ $value }}%"
```

### 9.3 Distributed Tracing

**Jaeger Tracing Example**:

```python
# auth-service/src/tracing.py
from opentelemetry import trace
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

tracer_provider = TracerProvider(
    resource=Resource.create({"service.name": "auth-service"})
)
jaeger_exporter = JaegerExporter(
    agent_host_name="jaeger-agent.faultmaven-observability.svc.cluster.local",
    agent_port=6831
)
tracer_provider.add_span_processor(BatchSpanProcessor(jaeger_exporter))
trace.set_tracer_provider(tracer_provider)

# Instrument FastAPI
FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)

# Example traced function
@trace.get_tracer(__name__).start_as_current_span("create_user")
async def create_user(username: str, email: str):
    span = trace.get_current_span()
    span.set_attribute("user.username", username)

    # Sub-span for database operation
    with trace.get_tracer(__name__).start_as_current_span("db.insert_user"):
        user = await db.create_user(username, email)

    # Sub-span for event publishing
    with trace.get_tracer(__name__).start_as_current_span("event.publish"):
        await event_bus.publish(Event(type="user.created", payload={"user_id": user.id}))

    return user
```

**Correlation ID Propagation**:

```python
# middleware/correlation_id.py
from fastapi import Request
import uuid

async def correlation_id_middleware(request: Request, call_next):
    # Extract or generate correlation ID
    correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())

    # Store in context for logging
    request.state.correlation_id = correlation_id

    # Add to all outgoing requests
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id

    return response
```

### 9.4 Circuit Breakers

**Using Resilience4j Pattern**:

```python
# utils/circuit_breaker.py
from enum import Enum
import asyncio
import time
from typing import Callable, Any

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        timeout: int = 60,
        success_threshold: int = 2
    ):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.success_threshold = success_threshold
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
        self.state = CircuitState.CLOSED

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        if self.state == CircuitState.OPEN:
            if time.time() - self.last_failure_time > self.timeout:
                self.state = CircuitState.HALF_OPEN
                self.success_count = 0
            else:
                raise CircuitBreakerOpenError("Circuit breaker is OPEN")

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise

    def _on_success(self):
        self.failure_count = 0
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.success_threshold:
                self.state = CircuitState.CLOSED

    def _on_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN

# Usage example
case_service_circuit_breaker = CircuitBreaker(
    failure_threshold=5,
    timeout=60,
    success_threshold=2
)

async def get_case_with_circuit_breaker(case_id: str):
    try:
        return await case_service_circuit_breaker.call(
            case_client.get_case,
            case_id
        )
    except CircuitBreakerOpenError:
        # Return cached data or degraded response
        return get_cached_case(case_id)
```

---

## 10. Success Metrics

### 10.1 Technical Metrics (Week 12)

**Migration Completeness**:
- [ ] 8 core microservices deployed independently
- [ ] 100% of monolith functionality migrated
- [ ] Monolith decommissioned
- [ ] All services meet SLOs

**Performance**:
- [ ] P99 latency <5% regression vs. monolith
- [ ] Throughput >95% of monolith capacity
- [ ] Error rate <1% across all services
- [ ] Database query performance optimized

**Reliability**:
- [ ] Zero-downtime deployments verified
- [ ] Chaos engineering tests passed
- [ ] Disaster recovery procedures tested
- [ ] Multi-AZ failover working

**Observability**:
- [ ] 100% API contract coverage
- [ ] Distributed tracing end-to-end
- [ ] SLO dashboards for all services
- [ ] Alerting rules configured

### 10.2 Operational Metrics (3 Months)

**Deployment Velocity**:
- [ ] Daily deployments for >80% of services
- [ ] <15 minute CI/CD pipeline
- [ ] <5% deployment rollback rate
- [ ] Independent service deployments (no coordinated releases)

**Service Health**:
- [ ] 99.9% uptime for Tier 1 services
- [ ] 99.5% uptime for Tier 2 services
- [ ] <15 minute mean time to recovery (MTTR)
- [ ] <1% cross-service API errors

**Development Velocity**:
- [ ] 50% reduction in time-to-production for new features
- [ ] 3+ teams working independently
- [ ] <10% code conflicts between teams
- [ ] Test coverage >80% per service

### 10.3 Business Metrics (6 Months)

**Scalability**:
- [ ] 10x traffic capacity vs. monolith
- [ ] <30% infrastructure cost increase
- [ ] Auto-scaling working for all services
- [ ] Multi-region deployment capability

**Customer Impact**:
- [ ] No customer-facing downtime during migration
- [ ] <5% customer support ticket increase
- [ ] API response time improvement (p95 latency)
- [ ] 100% feature parity with monolith

**Enterprise Readiness**:
- [ ] 10+ enterprise customers on microservices
- [ ] Multi-tenant isolation verified
- [ ] Compliance audit passed (SOC 2, GDPR)
- [ ] Tiered service plans operational

---

## Appendix A: Repository Structure

### Service Repository Template

```
fm-{service-name}/
├── src/
│   ├── main.py                 # FastAPI application
│   ├── routes/                 # API endpoints
│   │   ├── __init__.py
│   │   └── {resource}.py
│   ├── domain/                 # Business logic
│   │   ├── __init__.py
│   │   ├── models.py
│   │   └── service.py
│   ├── persistence/            # Database access
│   │   ├── __init__.py
│   │   └── repository.py
│   └── utils/
│       ├── tracing.py
│       └── circuit_breaker.py
├── migrations/                 # Alembic migrations (if DB owner)
│   ├── env.py
│   └── versions/
│       └── 001_initial_schema.py
├── openapi/                    # API contract
│   └── api.yaml
├── chart/                      # Helm chart
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── deployment.yaml
│       ├── service.yaml
│       ├── ingress.yaml
│       └── configmap.yaml
├── tests/
│   ├── unit/
│   ├── integration/
│   └── contract/
├── Dockerfile
├── pyproject.toml
├── README.md
└── .github/
    └── workflows/
        └── ci.yml
```

---

## Appendix B: Decision Log

### Key Architectural Decisions

**ADR-001: Use Strangler Pattern for Migration**
- **Decision**: Gradual migration with API gateway routing
- **Rationale**: Zero-downtime, low-risk, reversible
- **Alternatives Considered**: Big-bang rewrite, feature flags
- **Status**: Approved

**ADR-002: Single-Writer Data Ownership**
- **Decision**: Each service exclusively owns its database
- **Rationale**: Clear boundaries, independent scaling, schema evolution
- **Alternatives Considered**: Shared database, CQRS with event sourcing
- **Status**: Approved

**ADR-003: Event-Driven Architecture for Cross-Service Communication**
- **Decision**: Use event bus for non-critical async workflows
- **Rationale**: Loose coupling, eventual consistency, resilience
- **Alternatives Considered**: Synchronous API calls only, CQRS
- **Status**: Approved

**ADR-004: API Gateway for Traffic Management**
- **Decision**: Kong or NGINX Ingress for routing
- **Rationale**: Centralized auth, rate limiting, traffic splitting
- **Alternatives Considered**: Service mesh only, client-side routing
- **Status**: Approved

---

## Appendix C: Glossary

- **Bounded Context**: A logical boundary within which a domain model is defined
- **Circuit Breaker**: Pattern to prevent cascading failures by stopping calls to failing services
- **Correlation ID**: Unique identifier propagated across service calls for tracing
- **Dual-Write**: Writing to both old and new systems during migration
- **Event Sourcing**: Storing state changes as a sequence of events
- **SLO**: Service Level Objective - target for service performance
- **Strangler Pattern**: Gradually replacing old system by routing traffic to new services

---

**Document Version**: 1.0
**Last Updated**: 2025-11-14
**Status**: Ready for Execution
**Next Review**: 2025-12-14 (after Phase 1 completion)

**Change Log**:
- 2025-11-14: Initial version with complete bounded context decomposition, strangler pattern migration strategy, and operational readiness plans
