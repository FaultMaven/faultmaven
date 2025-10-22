# FaultMaven Documentation Map and Status

**Version:** 1.0  
**Last Updated:** 2025-10-10  
**Purpose:** Navigation guide and creation roadmap for all architecture documentation

---

## Document Organization Structure

This map shows all FaultMaven design documents organized by functional area, with status indicators.

**Status Legend**:
- ✅ **Exists** - Document created and maintained
- 📝 **To be created** - Document planned, buckets created
- 🎯 **Authoritative** - Source of truth for this domain
- 🔄 **Legacy** - Historical, superseded by newer design

---

## 1. Foundation Documents

| Document | Status | Description | Priority |
|----------|--------|-------------|----------|
| System Requirements Specification (SRS) v2.0 | ✅ 🎯 | 62 requirements: Response Types, Case Management, Performance, Security | CRITICAL |
| Investigation Phases and OODA Integration Framework v2.1 | ✅ 🎯 | 7-phase lifecycle (0-6), OODA steps, engagement modes, state management | CRITICAL |

**Purpose**: Define WHAT the system must do (SRS) and HOW investigations flow (Framework)

---

## 2. Core Investigation Components

| Document | Status | Description | Priority |
|----------|--------|-------------|----------|
| Evidence Collection and Tracking Design v2.1 | ✅ 🎯 | Evidence schemas, 5D classification, strategies, agent prompts | CRITICAL |
| Case Lifecycle Management v1.0 | ✅ 🎯 | Case status state machine (7 states), transition rules, stall detection | HIGH |
| Case and Session Concepts | ✅ | Case vs Session distinction, multi-session architecture | HIGH |
| Conversation Intelligence Design | 📝 | Circular dialogue detection, progress measurement, dead-end prevention | HIGH |

**Purpose**: Core investigation mechanics and case management

**To Create - Conversation Intelligence Design**:
- Circular dialogue detection algorithms (FR-CNV-002 requirement)
- Progressive dialogue measurement (FR-CNV-003 requirement)
- Information completeness tracking
- Dead-end detection and recovery
- Conversation phase advancement triggers

---

## 3. AI Agent Architecture

| Document | Status | Description | Priority |
|----------|--------|-------------|----------|
| Investigation Phases and OODA Integration Framework v2.1 | ✅ 🎯 | **IMPLEMENTED**: 7-phase investigation, OODA engine, phase handlers | CRITICAL |
| Query Classification and Prompt Engineering | ✅ | 17 intent taxonomy, 9 ResponseTypes, pattern classification | HIGH |
| Prompt Engineering Architecture | 📝 | Multi-layer prompting, optimization, version management | MEDIUM |
| Agent Orchestration Design | ✅ | Agent workflow coordination, reasoning engine | MEDIUM |
| Planning System Architecture | 📝 | Problem decomposition, strategic planning, risk assessment | MEDIUM |

**Purpose**: AI agent intelligence, classification, and reasoning

**To Create - Prompt Engineering Architecture**:
- Multi-layer prompt architecture (6 layers)
- Dynamic prompt assembly
- Phase-aware prompt selection
- Token optimization (81% reduction details)
- Version management and A/B testing

**To Create - Planning System Architecture**:
- Problem decomposition algorithms
- Strategic planning workflows
- Risk assessment framework
- Alternative solution evaluation
- Adaptive execution patterns

---

## 4. API, Schema, and Data Design

| Document | Status | Description | Priority |
|----------|--------|-------------|----------|
| Schema v3.1.0 Design | 📝 | v3.1.0 API schema, AgentResponse, ViewState, validation | CRITICAL |
| Data Submission Design | ✅ | 10K limit, pattern matching, async/sync processing | MEDIUM |
| Data Flow Architecture | 📝 | End-to-end request lifecycle, middleware, response assembly | HIGH |
| API Contracts and Integration | 📝 | REST endpoints, formats, error codes, versioning | MEDIUM |

**Purpose**: API contracts, data models, and integration specifications

**To Create - Schema v3.1.0 Design**:
- Complete AgentResponse schema
- ViewState structure and fields
- Source attribution model
- Schema validation rules
- Request/response examples
- Breaking changes from v3.0

**To Create - Data Flow Architecture**:
- Complete request lifecycle (from client to response)
- Middleware processing pipeline
- Memory retrieval and consolidation flows
- Service layer orchestration
- Error context propagation
- Correlation ID tracking

**To Create - API Contracts**:
- All REST endpoint specifications
- Request/response format standards
- Error code taxonomy
- HTTP status code usage
- API versioning strategy
- Backward compatibility rules

---

## 5. Session and Authentication

| Document | Status | Description | Priority |
|----------|--------|-------------|----------|
| Session Management Specification | ✅ | Multi-session, client-based resumption, Redis multi-index | HIGH |
| Authentication Design | ✅ | Auth architecture, user management, token handling | HIGH |
| Authorization and Access Control | 📝 | RBAC, case ownership, permissions, audit logging | MEDIUM |

**Purpose**: User identity, session management, and access control

**To Create - Authorization and Access Control**:
- RBAC model (roles: User, Admin, Support, Auditor)
- Case ownership and sharing model
- Permission checking architecture
- Audit logging for authorization decisions
- Case access control matrix

---

## 6. Infrastructure Layer

| Document | Status | Description | Priority |
|----------|--------|-------------|----------|
| Dependency Injection System | ✅ | DI container, service interfaces, lifecycle management | HIGH |
| LLM Provider Integration | 📝 | Multi-provider routing, failover, cost tracking | HIGH |
| Observability and Tracing | 📝 | Distributed tracing, metrics, monitoring, alerting | HIGH |
| Persistence Layer Design | 📝 | Redis, ChromaDB, data durability, backup | HIGH |
| Memory Management Architecture | 📝 | Memory hierarchy, consolidation, decay mechanisms | MEDIUM |

**Purpose**: Infrastructure services and cross-cutting concerns

**To Create - LLM Provider Integration**:
- Provider abstraction interface
- OpenAI, Anthropic, Fireworks implementations
- Automatic failover logic
- Cost tracking and budgeting
- Token usage monitoring
- Provider selection algorithms

**To Create - Observability and Tracing**:
- Opik integration architecture
- Distributed tracing design
- Metrics collection (system, application, business)
- Performance monitoring
- Alert manager configuration
- SLA monitoring

**To Create - Persistence Layer Design**:
- Redis session store implementation
- ChromaDB vector store design
- Data durability guarantees
- Backup and recovery procedures
- Multi-region replication
- Storage optimization

**To Create - Memory Management Architecture**:
- Memory hierarchy (working/session/user/episodic)
- Memory consolidation algorithms
- Semantic embeddings and retrieval
- Decay mechanisms and relevance scoring
- Cross-session learning
- Token budget management

---

## 7. Data Processing and Knowledge

| Document | Status | Description | Priority |
|----------|--------|-------------|----------|
| Data Processing Pipeline | 📝 | File classification, insight extraction, async workflows | MEDIUM |
| Knowledge Base Architecture | 📝 | RAG, document ingestion, semantic search, ChromaDB | MEDIUM |
| Log Analysis and Classification | 📝 | Log parsing, anomaly detection, pattern recognition | MEDIUM |

**Purpose**: Data analysis, knowledge management, and insight extraction

**To Create - Data Processing Pipeline**:
- File upload and validation
- Automatic classification algorithms
- Insight extraction engines
- Processing status tracking
- Async job management
- Large file handling (>10MB)

**To Create - Knowledge Base Architecture**:
- RAG implementation design
- Document ingestion pipeline
- Embedding generation (BGE-M3)
- Semantic search algorithms
- ChromaDB integration
- Knowledge base updates and versioning

**To Create - Log Analysis**:
- Log parsing strategies
- Structured vs unstructured logs
- Anomaly detection algorithms
- Pattern recognition ML models
- Error extraction and classification
- Timeline reconstruction

---

## 8. Security and Privacy

| Document | Status | Description | Priority |
|----------|--------|-------------|----------|
| Security Architecture and Policies | 📝 | PII protection, sanitization, encryption, audit | HIGH |
| Protection Systems | 📝 | Rate limiting, circuit breakers, anomaly detection | MEDIUM |
| Compliance and Data Governance | 📝 | GDPR/CCPA, data retention, audit requirements | MEDIUM |

**Purpose**: Security, privacy, and regulatory compliance

**To Create - Security Architecture**:
- PII detection and redaction (Presidio)
- Data sanitization pipeline
- Encryption at rest and in transit
- Audit logging architecture
- Security monitoring
- Incident response procedures

**To Create - Protection Systems**:
- Rate limiting implementation
- Circuit breaker patterns
- Anomaly detection (behavioral analysis)
- Reputation management
- DDoS protection
- Request validation and sanitization

**To Create - Compliance and Governance**:
- GDPR compliance architecture
- CCPA compliance architecture
- Data retention policies
- Right to deletion implementation
- Data export/portability
- Consent management

---

## 9. Performance and Operations

| Document | Status | Description | Priority |
|----------|--------|-------------|----------|
| Performance and Scalability Design | 📝 | Response times, throughput, horizontal scaling, caching | HIGH |
| Deployment Architecture | 📝 | Container strategy, dependencies, config management, HA | HIGH |
| Health Monitoring and SLA | 📝 | Health checks, component monitoring, SLA tracking | MEDIUM |

**Purpose**: System performance, deployment, and operational excellence

**To Create - Performance and Scalability**:
- Response time targets (by operation type)
- Throughput capabilities and benchmarks
- Horizontal scaling strategies
- Caching strategy (LLM, KB, session, planning, file)
- Resource management (connection pooling, rate limiting)
- Load balancing architecture

**To Create - Deployment Architecture**:
- Container strategy (Docker, Kubernetes)
- Multi-stage build optimization
- External dependencies (Redis, ChromaDB, Presidio, Opik)
- Configuration management
- Environment-specific configs
- High availability design
- Graceful shutdown procedures

**To Create - Health Monitoring and SLA**:
- Liveness, readiness, startup probes
- Component health checks
- Dependency monitoring
- SLA definition and tracking
- Alert rules and thresholds
- Incident response integration

---

## 10. Implementation Reference

| Document | Status | Description | Priority |
|----------|--------|-------------|----------|
| Implementation Module Mapping | 📝 | File-by-file code organization breakdown | HIGH |
| Design Patterns Guide | 📝 | Implementation patterns with code examples | MEDIUM |
| Service Layer Patterns | ✅ | Service implementation patterns | MEDIUM |
| Interface-Based Design Guide | ✅ | Interface definitions and guidelines | MEDIUM |

**Purpose**: Code organization and implementation patterns

**To Create - Implementation Module Mapping**:
- Complete directory structure breakdown
- API Layer: `main.py`, `api/middleware/`, `api/v1/routes/`
- Service Layer: All services with responsibilities
- Agentic Framework: 7 components mapped to files
- Core Domain: Agent, processing, knowledge, tools
- Infrastructure: LLM, security, observability, persistence
- Data Models: Interfaces, agentic, API models
- Configuration: Settings, feature flags, container

**To Create - Design Patterns Guide**:
- Interface Segregation (with code examples)
- Dependency Inversion (with code examples)
- Command Query Separation (with code examples)
- Single Responsibility examples
- Error Context Propagation patterns
- Transaction boundary management

---

## 11. Developer Guides and References

| Document | Status | Description | Priority |
|----------|--------|-------------|----------|
| Context Management Guide | ✅ | QueryContext usage, typed context system | MEDIUM |
| Token Estimation Guide | ✅ | Provider-specific tokenizers, cost optimization | MEDIUM |
| Container Usage Guide | ✅ | DI container usage, service registration | MEDIUM |
| Developer Guide | ✅ | Getting started, setup, development workflow | LOW |
| Testing Guide | ✅ | Test strategy, fixtures, mocking patterns | MEDIUM |

**Purpose**: Developer onboarding and implementation guides

---

## 12. Supporting Documentation

| Document | Status | Description | Priority |
|----------|--------|-------------|----------|
| Architecture Evolution | ✅ | Architecture history, major migrations | LOW |
| Agentic Framework Migration Guide | ✅ | Migration from legacy to agentic | LOW |
| Configuration System Refactor | ✅ | Configuration centralization | LOW |
| Doctor-Patient Prompting v1.0 | ✅ 🔄 | Legacy prompting (superseded) | LOW |
| Sub-Agent Architecture v1.0 | ✅ 🔄 | Legacy multi-agent (superseded) | LOW |
| System Architecture v1.0 | ✅ 🔄 | Original architecture (superseded) | LOW |

**Purpose**: Historical context and migration guidance

---

## Creation Priority Matrix

### Critical (Create First)

These documents are referenced extensively and needed for complete system understanding:

1. **Schema v3.1.0 Design** - Core API contract
2. **Conversation Intelligence Design** - Critical SRS requirements (FR-CNV-002, FR-CNV-003)
3. **Data Flow Architecture** - Understanding request processing

### High Priority (Create Soon)

Essential for implementation and operations:

4. **Implementation Module Mapping** - Code organization reference
5. **LLM Provider Integration** - Critical infrastructure
6. **Observability and Tracing** - Operations requirement
7. **Persistence Layer Design** - Data storage architecture
8. **Performance and Scalability Design** - Non-functional requirements
9. **Deployment Architecture** - Operational requirement

### Medium Priority (Create as Needed)

Important but not blocking:

10. **Prompt Engineering Architecture** - Already partially documented
11. **Planning System Architecture** - Advanced feature
12. **Design Patterns Guide** - Developer reference
13. **Data Processing Pipeline** - Feature enhancement
14. **Knowledge Base Architecture** - RAG details
15. **Security Architecture** - Expand existing coverage
16. **Authorization and Access Control** - Future enhancement

### Lower Priority (As System Evolves)

17-20. Other supporting documents as needed

---

## Document Dependencies

### Critical Path

```
System Requirements (SRS)
    ↓
Investigation Phases Framework
    ├── Evidence Collection Design
    ├── Case Lifecycle Management
    └── Schema v3.1.0 Design
        └── Data Flow Architecture
            └── Implementation Module Mapping
```

### Infrastructure Path

```
Dependency Injection System (exists)
    ├── LLM Provider Integration
    ├── Persistence Layer Design
    ├── Observability and Tracing
    └── Memory Management Architecture
```

### Security Path

```
Authentication Design (exists)
    ├── Authorization and Access Control
    └── Security Architecture
        └── Compliance and Data Governance
```

---

## Quick Start Navigation

**New to FaultMaven?** Read these in order:
1. System Requirements Specification (understand WHAT)
2. Architecture Overview (THIS DOCUMENT - understand overall structure)
3. Investigation Phases Framework (understand investigation process)
4. Evidence Collection Design (understand data models)

**Implementing a feature?** Find your area:
- **API/Schema**: Schema v3.1.0 Design, Data Flow Architecture
- **Investigation**: Investigation Phases, Evidence Collection
- **Infrastructure**: DI System, LLM Integration, Observability
- **Security**: Authentication, Authorization, Security Architecture
- **Data**: Data Processing, Knowledge Base, Log Analysis

**Troubleshooting the system?**
- Observability and Tracing (monitoring, logs, metrics)
- Health Monitoring and SLA (component health)
- Performance and Scalability (optimization)

---

## Document Creation Template

When creating new design documents, use this structure:

```markdown
# [Document Title]
## [Subtitle]

**Document Type:** [Design Specification | Component Specification | Process Framework]
**Version:** [X.Y]
**Last Updated:** [YYYY-MM-DD]
**Status:** [Design | Implementation | Deprecated]
**Parent/Related:** [Links to related docs]

---

## Document Scope and Authority

### What This Document Covers
[Clear statement of scope]

### What This Document Does NOT Cover
[Clear boundaries, reference other docs]

### Related Design Documents
[Parent/child relationships]

---

[Main Content with clear sections]

---

## Document Metadata
[Version history, audience, prerequisites]
```

---

## Maintenance Guidelines

### Document Ownership

Each document should have:
- Clear scope (WHAT it covers)
- Clear authority (WHO owns this domain)
- Clear references (WHERE related info lives)
- Version history (WHEN it changed)

### Cross-Reference Standards

When referencing other documents:
```markdown
See [Document Name](./path/to/doc.md#section-anchor) for [specific aspect]
```

### Update Coordination

When updating related documents:
1. Identify all cross-references
2. Update all affected documents
3. Verify no broken links
4. Update version numbers
5. Add to document history

---

## Statistics

**Total Documents Planned**: 40+  
**Currently Exist**: 20 (50%)  
**Critical Priority**: 3 to create  
**High Priority**: 6 to create  
**Medium Priority**: 8 to create

**Status by Category**:
- Foundation: 2/2 (100% ✅)
- Investigation Components: 3/4 (75%)
- AI Agent: 4/5 (80%) - OODA Framework implemented, legacy spec deprecated
- API/Schema: 1/4 (25%)
- Session/Auth: 2/3 (67%)
- Infrastructure: 1/5 (20%)
- Data Processing: 0/3 (0%)
- Security: 0/3 (0%)
- Operations: 0/3 (0%)
- Implementation: 2/4 (50%)
- Developer Guides: 5/5 (100% ✅)

---

**Document Version**: 1.0  
**Maintained By**: Architecture Team  
**Review Cycle**: Monthly  
**Next Review**: 2025-11-10











