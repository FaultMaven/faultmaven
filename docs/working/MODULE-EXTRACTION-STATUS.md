# FaultMaven Module Extraction Status

**Last Updated**: 2026-01-06
**Overall Progress**: 71% complete (5 of 7 vertical slices extracted)

---

## Overview

FaultMaven is undergoing a systematic transformation into a modular monolith architecture using **vertical slice architecture**. Each module owns its complete feature stack from API → Domain → Infrastructure.

**Architecture Pattern**:
```
faultmaven/modules/{name}/
├── api/              # REST API endpoints and route definitions
├── domain/           # Business logic (services, models, schemas)
└── infrastructure/   # Infrastructure implementations (repositories, external clients)
```

---

## Module Extraction Progress

### ✅ Completed Modules (5/7)

#### 1. Auth Module
- **Status**: ✅ Complete (pushed to remote)
- **Files**: 31 files (5 API routes, 7 models, 5 services, 4 repos, 4 stores)
- **Complexity**: Medium
- **Documentation**: `AUTH-MODULE-EXTRACTION-COMPLETE.md`
- **Key Changes**:
  - Renamed `SessionService` → `AuthSessionService` (clarity with Case module's InvestigationSession)
  - Fixed circular imports by removing eager imports from `__init__.py` files
  - Updated 104 files with new import paths
- **Commits**: 3 commits
  - `4d595bf4` - Extract Auth module with vertical slice architecture
  - `d52a7842` - Resolve circular import issues
  - `3a630583` - Add comprehensive documentation

#### 2. Case Module
- **Status**: ✅ Complete
- **Files**: ~25-30 files
- **Complexity**: Medium-High
- **Key Components**:
  - Case management (create, update, search)
  - Investigation sessions (moved from Session module)
  - Case-specific domain models and services

#### 3. Evidence Module
- **Status**: ✅ Complete
- **Files**: ~20 files
- **Complexity**: Medium
- **Key Components**:
  - Evidence upload and storage
  - File metadata management
  - Evidence retrieval and search

#### 4. Knowledge Module
- **Status**: ✅ Complete
- **Files**: ~30 files
- **Complexity**: Medium-High
- **Documentation**: `KNOWLEDGE-MODULE-ARCHITECTURE.md`
- **Key Components**:
  - Knowledge base management
  - RAG (Retrieval Augmented Generation)
  - Vector search with ChromaDB
  - Semantic search

#### 5. Report Module
- **Status**: ✅ Complete
- **Files**: ~15 files
- **Complexity**: Low-Medium
- **Key Components**:
  - Report generation
  - Report templates
  - Export functionality

---

### ⏳ Remaining Modules (2/7)

#### 6. Agent Module (PLANNED - NOT STARTED)
- **Status**: 📋 Detailed plan created, awaiting team review
- **Estimated Effort**: 3-5 weeks (most complex module)
- **Files**: 35-40 production files + 49 test files (~6,000-8,000 LOC production, ~5,000-7,000 LOC tests)
- **Complexity**: ⚠️ **VERY HIGH** - Most complex extraction
- **Documentation**: `AGENT-MODULE-EXTRACTION-PLAN.md` (comprehensive)
- **Key Challenges**:
  1. **Three Overlapping Services** requiring clarification:
     - `AgentOrchestrationService` (1,068 LOC) - Low-level agent execution
     - `InvestigationOrchestrator` (911 LOC) - Mid-level workflow orchestration
     - `InvestigationService` (369 LOC) - High-level investigation management
  2. **Shared Core Investigation Components** (~2,000 LOC in `core/investigation/`)
     - OODA engine, Milestone engine, Phase definitions
     - Solution: Create shared `faultmaven/investigation/core/` library
  3. **Tool System Spanning Multiple Modules**
     - Evidence tools, Knowledge tools, Web search tools
     - Solution: Distributed tools pattern with plugin discovery
  4. **Strong LLM Infrastructure Coupling**
     - LLM providers, streaming, structured output
     - Multiple provider implementations
  5. **Session Management Coupling**
     - Agent sessions vs Investigation sessions vs Auth sessions
     - Needs careful dependency management

**Recommended Module Structure**:
```
faultmaven/modules/agent/
├── api/
│   ├── __init__.py
│   ├── agent.py              # Main agent endpoints
│   └── streaming.py          # Streaming response endpoints
├── domain/
│   ├── models/
│   │   ├── agent_state.py    # Agent execution state
│   │   ├── agent_config.py   # Agent configuration
│   │   ├── tool.py           # Tool abstractions
│   │   └── streaming.py      # Streaming models
│   ├── services/
│   │   ├── agent_orchestration_service.py  # Low-level execution
│   │   ├── investigation_orchestrator.py   # Mid-level workflow
│   │   └── investigation_service.py        # High-level management
│   └── events/
│       └── agent_events.py   # Domain events
└── infrastructure/
    ├── repositories/
    │   └── agent_repository.py
    ├── tools/
    │   ├── base_tool.py
    │   ├── tool_registry.py
    │   └── agent_tools/      # Agent-specific tools
    └── llm/
        └── agent_llm_client.py

# Shared Investigation Core (NEW)
faultmaven/investigation/
├── core/
│   ├── engines/
│   │   ├── ooda_engine.py
│   │   ├── milestone_engine.py
│   │   └── hypothesis_engine.py
│   ├── models/
│   │   ├── phases.py
│   │   ├── strategies.py
│   │   └── conclusions.py
│   └── coordinators/
│       └── investigation_coordinator.py
└── __init__.py
```

**5-Phase Implementation Plan**:
1. **Phase 1**: Preparation & Investigation Core library (Week 1)
2. **Phase 2**: Infrastructure & Events (Week 2)
3. **Phase 3**: Domain Models & Services - resolve overlap (Week 3)
4. **Phase 4**: Tool System & API - distributed tools pattern (Week 4)
5. **Phase 5**: Testing & Finalization (Week 5)

**Dependencies**:
- **Uses**: Case (investigations), Evidence (tools), Knowledge (RAG tools), Session (management), LLM (providers)
- **Used By**: API routes, Frontend

**Risks**:
- High complexity with overlapping services
- Strong coupling to LLM infrastructure
- Tool system refactoring required
- Extensive test suite migration

**Next Action**: Team review of `AGENT-MODULE-EXTRACTION-PLAN.md` and Phase 1 kickoff approval

---

#### 7. Session Module (OPTIONAL - MAY BE DISTRIBUTED)
- **Status**: ⚠️ Under architectural review
- **Complexity**: Medium
- **Note**: Some session functionality has been distributed:
  - Auth sessions → Auth module (AuthSessionService)
  - Investigation sessions → Case module
  - General session management may remain as shared infrastructure

**Decision Needed**:
- Extract remaining session functionality as standalone module?
- Or distribute remaining components to relevant modules?
- Or keep as shared infrastructure layer?

---

## Overall Statistics

| Metric | Value |
|--------|-------|
| **Total Modules** | 7 planned |
| **Completed** | 5 modules (71%) |
| **Remaining** | 2 modules (29%) |
| **Total Files Migrated** | ~150+ files |
| **Import Updates** | ~200+ files |
| **Test Files** | ~100+ test files |

---

## Architecture Principles

### 1. Vertical Slice Architecture
Each module owns its complete feature stack:
- **API Layer**: FastAPI routes, request/response models
- **Domain Layer**: Business logic, domain models, services
- **Infrastructure Layer**: Repositories, external clients, storage

### 2. Clear Module Boundaries
- Modules communicate through well-defined interfaces
- No circular dependencies between modules
- Explicit dependency direction (Agent depends on Case/Evidence/Knowledge, not reverse)

### 3. Import Strategy
- **Avoid eager imports** in `__init__.py` files (prevents circular imports)
- Import components directly from source files when needed
- Only declare `__all__` for documentation

### 4. The Shuffle Pattern
- Use `git mv` to preserve git history during refactoring
- Move files first, then update imports
- Commit file moves separately from logic changes

### 5. Service Naming Clarity
- Rename services when context changes (e.g., `SessionService` → `AuthSessionService`)
- Avoid ambiguous names across modules
- Prefix with module context when needed

---

## Known Patterns and Solutions

### Circular Import Resolution
**Problem**: Eager imports in `__init__.py` create circular dependencies

**Solution**:
```python
# ❌ BAD - Eager import
from .api import router
from .domain import services
from .infrastructure import repositories

# ✅ GOOD - Lazy loading
# Don't eagerly import to avoid circular imports
# Components will be imported directly when needed

__all__ = ["router", "services", "repositories"]
```

### Service Overlap Resolution
**Problem**: Multiple services with similar responsibilities

**Solution**: Clear separation of concerns by abstraction level:
- **Low-level**: Execution primitives (LLM calls, tool execution)
- **Mid-level**: Workflow orchestration (hypothesis tracking, phase progression)
- **High-level**: Business operations (investigation management, milestones)

### Shared Components
**Problem**: Components used by multiple modules (e.g., Investigation Core)

**Solution**: Extract to shared library outside module boundaries:
```
faultmaven/
├── modules/          # Vertical slices
│   ├── auth/
│   ├── case/
│   └── agent/
├── investigation/    # Shared investigation framework
│   └── core/
└── core/            # Other shared infrastructure
```

---

## Migration Process

### Standard Extraction Steps

For each module extraction:

1. **Planning**
   - Identify all components (services, models, API routes, repositories)
   - Analyze dependencies (what it uses, what uses it)
   - Document architectural challenges and solutions
   - Create phased implementation plan

2. **Structure Creation**
   - Create module directory structure
   - Set up `__init__.py` files (no eager imports!)

3. **File Migration**
   - Use `git mv` to preserve history
   - Move files in logical groups (API → Domain → Infrastructure)
   - Commit file moves separately

4. **Import Updates**
   - Update all import statements across codebase
   - Fix circular imports
   - Update dependency injection

5. **Testing**
   - Run full test suite
   - Fix failing tests with updated imports
   - Verify no regressions

6. **Documentation**
   - Create extraction completion document
   - Update this status document
   - Document architectural decisions

7. **Commit & Push**
   - Create semantic commits
   - Push to remote
   - Update team

---

## References

### Module-Specific Documentation
- Auth Module: `AUTH-MODULE-EXTRACTION-COMPLETE.md`
- Knowledge Module: `KNOWLEDGE-MODULE-ARCHITECTURE.md`
- Agent Module: `AGENT-MODULE-EXTRACTION-PLAN.md`

### Architecture Documentation
- Main Architecture: `/home/swhouse/product/faultmaven/docs/README.md`
- Vertical Slice Pattern: Standard FastAPI modular architecture

---

## Timeline

- **Auth Module**: Completed 2026-01-06
- **Case Module**: Completed (prior to this session)
- **Evidence Module**: Completed (prior to this session)
- **Knowledge Module**: Completed (prior to this session)
- **Report Module**: Completed (prior to this session)
- **Agent Module**: Planned (awaiting team review, estimated 3-5 weeks)
- **Session Module**: Under architectural review

---

## Next Steps

1. **Immediate**: Team review of `AGENT-MODULE-EXTRACTION-PLAN.md`
2. **Short-term**: Approve Agent module architectural decisions
3. **Medium-term**: Begin Agent module Phase 1 (Investigation Core extraction)
4. **Long-term**: Complete Agent module extraction (Phases 2-5)
5. **Final**: Decide on Session module strategy

---

**Status**: Ready for Agent module extraction after team review and approval
**Completion**: 71% (5/7 modules)
**Next Milestone**: Agent module extraction (most complex, 3-5 weeks estimated)

<!-- DELETE WHEN: All 7 modules extracted and vertical slice architecture complete -->
