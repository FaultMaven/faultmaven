# Implementation Module Mapping

**Version:** 1.0
**Date:** 2025-10-24
**Purpose:** Complete file-by-file breakdown of FaultMaven codebase organization
**Total Files:** 249 Python files across 8 architectural layers

---

## Overview

This document maps every module in the FaultMaven codebase to its architectural layer and responsibility. Use this as a navigation guide when working with the implementation.

**Update Frequency**: 🔥 HIGH - Update when adding/moving/renaming modules

---

## Table of Contents

1. [API Layer](#1-api-layer) - HTTP endpoints, middleware, request/response handling
2. [Service Layer](#2-service-layer) - Business logic orchestration
3. [Agentic Framework](#3-agentic-framework) - AI agent components
4. [Core Domain](#4-core-domain) - Domain logic and algorithms
5. [Infrastructure Layer](#5-infrastructure-layer) - External integrations
6. [Data Models](#6-data-models) - Data structures and interfaces
7. [Configuration](#7-configuration) - Settings and dependency injection
8. [Utilities](#8-utilities) - Cross-cutting utilities

---

## 1. API Layer

**Location**: `faultmaven/api/`
**Responsibility**: HTTP interface, request validation, routing, middleware
**Entry Point**: `faultmaven/main.py` (FastAPI application)

### Main Application

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app initialization, router registration, startup/shutdown |

### API v1 Routes

**Location**: `faultmaven/api/v1/routes/`

| File | Endpoints | Purpose |
|------|-----------|---------|
| `agent.py` | `POST /api/v1/query` | Main troubleshooting query endpoint |
| `case.py` | `POST/GET/PUT/DELETE /api/v1/cases/*` | Case CRUD operations |
| `session.py` | `POST/GET/DELETE /api/v1/sessions/*` | Session management (auth) |
| `knowledge.py` | `POST/GET /api/v1/knowledge/*` | Knowledge base operations |
| `data.py` | `POST /api/v1/data` | File uploads and data submission |
| `health.py` | `GET /health`, `/ready` | Health checks (liveness/readiness) |
| `auth.py` | `POST /api/v1/auth/*` | Authentication endpoints |
| `user_kb.py` | `POST/GET/DELETE /api/v1/users/{id}/kb/*` | User knowledge base management |

### Middleware

**Location**: `faultmaven/api/middleware/`

| File | Purpose |
|------|---------|
| `correlation.py` | Correlation ID tracking for distributed tracing |
| `error_handler.py` | Global exception handling and error responses |
| `logging.py` | Request/response logging middleware |
| `rate_limiting.py` | Rate limiting and throttling |

### API Utilities

**Location**: `faultmaven/api/v1/utils/`

| File | Purpose |
|------|---------|
| `validators.py` | Request validation helpers |
| `response_builders.py` | Standardized response construction |

### Dependencies

**Location**: `faultmaven/api/v1/`

| File | Purpose |
|------|---------|
| `dependencies.py` | FastAPI dependency injection for routes (container access) |

---

## 2. Service Layer

**Location**: `faultmaven/services/`
**Responsibility**: Business logic orchestration, service coordination
**Pattern**: Service-oriented architecture with interface-based design

### Domain Services

**Location**: `faultmaven/services/domain/`

| File | Responsibility | Key Methods |
|------|----------------|-------------|
| `case_service.py` | Case lifecycle management | `create_case()`, `update_case()`, `get_case()` |
| `session_service.py` | Session management (auth context) | `create_session()`, `validate_session()` |
| `data_service.py` | Data upload processing | `upload_data()`, `classify_data()` |
| `knowledge_service.py` | Knowledge base operations | `search()`, `add_document()` |
| `planning_service.py` | Strategic planning and execution plans | `create_execution_plan()` |
| `report_generation_service.py` | Document generation (Phase 6) | `generate_reports()`, `create_runbook()` |
| `report_recommendation_service.py` | Report recommendation logic | `recommend_report_type()` |

### Evidence Services

**Location**: `faultmaven/services/evidence/`

| File | Responsibility |
|------|----------------|
| `classification.py` | 5-dimensional LLM-based evidence classification |
| `lifecycle.py` | Evidence state management (REQUESTED → RECEIVED → VALIDATED) |
| `consumption.py` | Evidence consumption and integration |
| `evidence_factory.py` | Evidence request generation |
| `stall_detection.py` | Investigation stall detection (3+ blocked evidence) |
| `evidence_enhancements.py` | Evidence quality improvements |

### Preprocessing Services

**Location**: `faultmaven/services/preprocessing/`

| File | Responsibility |
|------|----------------|
| `preprocessing_service.py` | Orchestrates file preprocessing |
| `preprocessors/log_preprocessor.py` | Log file parsing and extraction |
| `preprocessors/generic_preprocessor.py` | Generic file handling |

---

## 3. Agentic Framework

**Location**: `faultmaven/services/agentic/`
**Responsibility**: AI agent orchestration, investigation framework
**Components**: 7 agentic framework components

### Orchestration

**Location**: `faultmaven/services/agentic/orchestration/`

| File | Responsibility |
|------|----------------|
| `agent_service.py` | Main agent orchestration (Plan→Execute→Observe→Re-plan) |
| `phase_orchestrator.py` | Phase advancement and transition logic |
| `ooda_integration.py` | OODA step execution and tracking |

### Phase Handlers (7 Phases)

**Location**: `faultmaven/services/agentic/phase_handlers/`

| File | Phase | OODA Profile | Purpose |
|------|-------|--------------|---------|
| `base.py` | N/A | N/A | Base class for all phase handlers |
| `intake_handler.py` | Phase 0 | Observe 50%, Orient 50% | Problem detection, offer investigation |
| `blast_radius_handler.py` | Phase 1 | Observe 60%, Orient 30% | Scope and impact assessment |
| `timeline_handler.py` | Phase 2 | Observe 60%, Orient 30% | Timeline establishment |
| `hypothesis_handler.py` | Phase 3 | Orient 35%, Decide 30% | Hypothesis generation |
| `validation_handler.py` | Phase 4 | Balanced 25% each | Hypothesis testing (full OODA) |
| `solution_handler.py` | Phase 5 | Act 35%, Decide 30% | Solution proposal |
| `document_handler.py` | Phase 6 | Orient 100% | Report generation and closure |

### Engines

**Location**: `faultmaven/services/agentic/engines/`

| File | Component | Purpose |
|------|-----------|---------|
| `workflow_engine.py` | Business Logic & Workflow Engine | Investigation workflow execution |
| `response_synthesizer.py` | Response Synthesizer | Multi-source response assembly |

### Management

**Location**: `faultmaven/services/agentic/management/`

| File | Component | Purpose |
|------|-----------|---------|
| `conversation_state_manager.py` | State & Session Manager | Hierarchical memory management |
| `context_manager.py` | Context Management | QueryContext assembly |
| `state_manager.py` | State Management | Investigation state tracking |
| `tool_broker.py` | Tool & Skill Broker | Dynamic tool orchestration |

### Safety

**Location**: `faultmaven/services/agentic/safety/`

| File | Component | Purpose |
|------|-----------|---------|
| `guardrails_layer.py` | Guardrails & Policy Layer | Security validation, PII protection |
| `error_manager.py` | Error Handling & Fallback Manager | Circuit breakers, error recovery |

### Hypothesis Management

**Location**: `faultmaven/services/agentic/hypothesis/`

| File | Purpose |
|------|---------|
| `systematic_generation.py` | Phase 3 systematic hypothesis generation |
| `opportunistic_capture.py` | Phases 0-2 opportunistic hypothesis capture |

---

## 4. Core Domain

**Location**: `faultmaven/core/`
**Responsibility**: Domain logic, algorithms, processing engines
**Pattern**: Pure domain logic with no infrastructure dependencies

### Investigation

**Location**: `faultmaven/core/investigation/`

| File | Purpose |
|------|---------|
| `memory_manager.py` | Hierarchical memory (hot/warm/cold tiers), LLM compression |
| `hypothesis_manager.py` | Hypothesis lifecycle, confidence decay, anchoring prevention |
| `investigation_coordinator.py` | Investigation orchestration |
| `strategy_selector.py` | Investigation strategy selection (ACTIVE_INCIDENT vs POST_MORTEM) |

### Processing

**Location**: `faultmaven/core/processing/`

| File | Purpose |
|------|---------|
| `log_analyzer.py` | Log file analysis and pattern extraction |
| `pattern_learner.py` | ML-based pattern recognition |
| `data_classifier.py` | Data type classification |
| `ooda_response_converter.py` | OODA response conversion to AgentResponse |

### Knowledge

**Location**: `faultmaven/core/knowledge/`

| File | Purpose |
|------|---------|
| `retrieval.py` | Knowledge retrieval from vector stores |
| `ingestion.py` | Document ingestion and chunking |

### Agent

**Location**: `faultmaven/core/agent/`

| File | Purpose |
|------|---------|
| `langchain_agent.py` | LangChain agent integration |
| `agent_factory.py` | Agent instance creation |

### Loop Guard

**Location**: `faultmaven/core/loop_guard/`

| File | Purpose |
|------|---------|
| `detector.py` | Circular dialogue detection |
| `breaker.py` | Loop breaking logic |

### Confidence

**Location**: `faultmaven/core/confidence/`

| File | Purpose |
|------|---------|
| `calculator.py` | Confidence score calculation |

### Reasoning

**Location**: `faultmaven/core/reasoning/`

| File | Purpose |
|------|---------|
| `query_understanding.py` | Query intent classification |

---

## 5. Infrastructure Layer

**Location**: `faultmaven/infrastructure/`
**Responsibility**: External integrations, cross-cutting concerns
**Pattern**: Infrastructure interfaces with pluggable implementations

### LLM Integration

**Location**: `faultmaven/infrastructure/llm/`

| File | Purpose |
|------|---------|
| `router.py` | Multi-provider LLM routing with failover |
| `token_counter.py` | Token counting (tiktoken, Anthropic) |
| `providers/openai_provider.py` | OpenAI integration |
| `providers/anthropic_provider.py` | Anthropic integration |
| `providers/fireworks_provider.py` | Fireworks AI integration |
| `providers/gemini_provider.py` | Google Gemini integration |
| `providers/huggingface_provider.py` | HuggingFace integration |
| `providers/openrouter_provider.py` | OpenRouter integration |
| `providers/local_provider.py` | Local LLM integration |

### Persistence

**Location**: `faultmaven/infrastructure/persistence/`

| File | Purpose |
|------|---------|
| `redis_session_store.py` | Redis session storage |
| `redis_session_manager.py` | High-level session operations |
| `case_vector_store.py` | ChromaDB vector storage for cases |
| `report_store.py` | Report persistence |

### Security

**Location**: `faultmaven/infrastructure/security/`

| File | Purpose |
|------|---------|
| `pii_redactor.py` | PII detection and redaction (Presidio integration) |
| `sanitizer.py` | Data sanitization |

### Knowledge

**Location**: `faultmaven/infrastructure/knowledge/`

| File | Purpose |
|------|---------|
| `vector_store.py` | ChromaDB vector store interface |
| `runbook_kb.py` | Runbook knowledge base management |
| `embeddings.py` | BGE-M3 embedding generation |

### Observability

**Location**: `faultmaven/infrastructure/observability/`

| File | Purpose |
|------|---------|
| `tracing.py` | Distributed tracing (Opik integration) |
| `metrics.py` | Metrics collection |

### Protection

**Location**: `faultmaven/infrastructure/protection/`

| File | Purpose |
|------|---------|
| `circuit_breaker.py` | Circuit breaker pattern |
| `rate_limiter.py` | Rate limiting |

### Concurrency

**Location**: `faultmaven/infrastructure/concurrency/`

| File | Purpose |
|------|---------|
| `lock_manager.py` | Distributed locking (report generation) |

### Jobs

**Location**: `faultmaven/infrastructure/jobs/`

| File | Purpose |
|------|---------|
| `background_tasks.py` | Async job execution |

### Tasks

**Location**: `faultmaven/infrastructure/tasks/`

| File | Purpose |
|------|---------|
| `case_cleanup.py` | Case cleanup tasks (orphan detection) |

### Health

**Location**: `faultmaven/infrastructure/health/`

| File | Purpose |
|------|---------|
| `checks.py` | Health check implementations |

### Auth

**Location**: `faultmaven/infrastructure/auth/`

| File | Purpose |
|------|---------|
| `provider.py` | Authentication provider |
| `user_manager.py` | User management |

### Caching

**Location**: `faultmaven/infrastructure/caching/`

| File | Purpose |
|------|---------|
| `cache_manager.py` | Caching layer (Redis) |

### Logging

**Location**: `faultmaven/infrastructure/logging/`

| File | Purpose |
|------|---------|
| `logger.py` | Structured logging setup |

### Monitoring

**Location**: `faultmaven/infrastructure/monitoring/`

| File | Purpose |
|------|---------|
| `prometheus.py` | Prometheus metrics |

### Telemetry

**Location**: `faultmaven/infrastructure/telemetry/`

| File | Purpose |
|------|---------|
| `collector.py` | Telemetry data collection |

---

## 6. Data Models

**Location**: `faultmaven/models/`
**Responsibility**: Data structures, interfaces, schemas
**Pattern**: Pydantic models with validation

### Core Models

| File | Purpose |
|------|---------|
| `api.py` | API request/response models (QueryRequest, AgentResponse, ViewState) |
| `common.py` | Shared models (SessionContext, DataInsightsResponse) |
| `case.py` | Case entity (Case, CaseMessage, CaseStatus, CasePriority) |
| `investigation.py` | Investigation state (InvestigationState, Hypothesis, OODAIteration, HierarchicalMemory) |
| `evidence.py` | Evidence models (EvidenceRequest, EvidenceProvided, EvidenceClassification) |
| `agentic.py` | Agentic framework models (QueryContext, PhaseContext) |
| `interfaces.py` | Service interfaces (ISessionStore, ICaseStore, ILLMProvider, etc.) |
| `report.py` | Report models (CaseReport, ReportType, RunbookMetadata) |

### Microservice Contracts

**Location**: `faultmaven/models/microservice_contracts/`

| File | Purpose |
|------|---------|
| `presidio.py` | Presidio PII service contracts |
| `opik.py` | Opik observability contracts |

---

## 7. Configuration

**Location**: `faultmaven/config/`
**Responsibility**: Settings, feature flags, dependency injection

| File | Purpose |
|------|---------|
| `settings.py` | Pydantic settings (environment variables, configuration) |
| `feature_flags.py` | Feature toggle management |

### Dependency Injection

| File | Purpose |
|------|---------|
| `container.py` | Dependency injection container (service instantiation) |

---

## 8. Utilities

**Location**: `faultmaven/` (root) and `faultmaven/utils/`
**Responsibility**: Cross-cutting utilities

| File | Purpose |
|------|---------|
| `exceptions.py` | Custom exception hierarchy |
| `utils/serialization.py` | JSON serialization helpers |
| `utils/validators.py` | Common validation functions |
| `utils/datetime_helpers.py` | DateTime utilities |

---

## 9. Tools

**Location**: `faultmaven/tools/`
**Responsibility**: LangChain tools for agent use

### Q&A Tools (Strategy Pattern)

| File | Purpose |
|------|---------|
| `document_qa_tool.py` | KB-neutral Document Q&A (core tool) |
| `case_evidence_qa.py` | Case Evidence Q&A wrapper |
| `user_kb_qa.py` | User KB Q&A wrapper |
| `global_kb_qa.py` | Global KB Q&A wrapper |

### KB Configs (Strategy Pattern)

**Location**: `faultmaven/tools/kb_configs/`

| File | Purpose |
|------|---------|
| `kb_config.py` | KBConfig abstract interface |
| `case_evidence_config.py` | Case evidence KB configuration |
| `user_kb_config.py` | User KB configuration |
| `global_kb_config.py` | Global KB configuration |

### Other Tools

| File | Purpose |
|------|---------|
| `web_search_tool.py` | Web search integration |
| `knowledge_base_tool.py` | Legacy KB tool |

---

## 10. Prompts

**Location**: `faultmaven/prompts/`
**Responsibility**: LLM prompt templates

| File | Purpose |
|------|---------|
| `phase_prompts.py` | 7-phase investigation prompts with OODA profiles |
| `response_prompts.py` | Response formatting prompts |
| `few_shot_examples.py` | Few-shot learning examples |
| `prompt_manager.py` | Dynamic prompt assembly |

### Investigation Prompts

**Location**: `faultmaven/prompts/investigation/`

| File | Purpose |
|------|---------|
| `lead_investigator.py` | Lead Investigator mode prompts |
| `consultant_mode.py` | Consultant mode prompts |
| `ooda_guidance.py` | OODA step guidance prompts |
| `strategy_prompts.py` | Investigation strategy prompts |

---

## 11. Scripts

**Location**: `faultmaven/scripts/`
**Responsibility**: Utility scripts

| File | Purpose |
|------|---------|
| `ingest_runbooks.py` | Runbook ingestion script |

---

## Architectural Layer Summary

| Layer | File Count | Directory | Purpose |
|-------|------------|-----------|---------|
| API Layer | ~20 | `api/` | HTTP interface |
| Service Layer | ~25 | `services/` | Business logic orchestration |
| Agentic Framework | ~25 | `services/agentic/` | AI agent components |
| Core Domain | ~20 | `core/` | Domain logic and algorithms |
| Infrastructure | ~45 | `infrastructure/` | External integrations |
| Data Models | ~15 | `models/` | Data structures |
| Configuration | ~3 | `config/`, `container.py` | Settings and DI |
| Tools | ~15 | `tools/` | LangChain tools |
| Prompts | ~10 | `prompts/` | LLM prompts |
| Utilities | ~5 | Root + `utils/` | Cross-cutting |
| **Total** | **~249** | | |

---

## Navigation Tips

### Finding a Feature

1. **API Endpoint**: Start at `api/v1/routes/`
2. **Business Logic**: Look in `services/domain/` or `services/agentic/`
3. **Domain Algorithm**: Check `core/`
4. **External Integration**: See `infrastructure/`
5. **Data Structure**: Review `models/`

### Adding a New Feature

1. **Define Model**: `models/*.py`
2. **Add Service**: `services/domain/*.py`
3. **Create API**: `api/v1/routes/*.py`
4. **Wire in Container**: `container.py`
5. **Update Tests**: `tests/` (mirror structure)

### Common Patterns

- **Service Layer**: Service → Core Domain → Infrastructure
- **API Layer**: Route → Service → Response
- **Agentic**: Phase Handler → OODA → Tools → LLM
- **Data Flow**: Request → Validation → Service → Domain → Infrastructure → Response

---

## Related Documentation

- [Architecture Overview](./architecture-overview.md) - High-level architecture
- [Interface-Based Design](./interface-based-design.md) - Interface definitions
- [Data Models Reference](./data-models-reference.md) - Comprehensive model catalog
- [Design Patterns Guide](./design-patterns-guide.md) - Implementation patterns

---

**Document Status**: ✅ Complete
**Last Updated**: 2025-10-24
**Maintainer**: Update when adding/moving/renaming modules
