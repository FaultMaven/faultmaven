# Agent Module Extraction Plan

**Date**: 2026-01-06
**Status**: Planning Phase
**Complexity**: HIGH (Final vertical slice)
**Estimated Effort**: 3-5 weeks (phased approach)

---

## Executive Summary

The Agent module is the **final and most complex** vertical slice extraction in the Platform Evolution. It encompasses AI agent orchestration, investigation workflows, tool systems, and core investigation components totaling **~35-40 production files** and **~49 test files**.

**Key Complexity Factors:**
- Strong coupling with LLM infrastructure
- Three overlapping investigation services requiring clarification
- Tool system spanning multiple modules (Evidence, Knowledge)
- Shared core investigation components (OODA, milestones, phases)
- Deep integration with Case, Evidence, Knowledge, and Session modules

**Recommended Approach:** Multi-phase extraction over 3-5 weeks with careful attention to module boundaries, shared infrastructure, and integration points.

---

## Current Platform Evolution Status

### Completed Modules: 5/7 (71%)
1. ✅ **Auth** - Authentication, authorization, users, sessions, teams, organizations, RBAC
2. ✅ **Case** - Case management + investigation sessions + data ingestion
3. ✅ **Evidence** - Evidence artifacts and file management
4. ✅ **Knowledge** - Knowledge base, RAG, vector search
5. ✅ **Report** - Reporting and analytics

### Remaining Modules: 2/7 (29%)
6. 🔄 **Agent** - Agent orchestration, investigation workflows (THIS DOCUMENT)
7. ⚠️ **Session** - Previously attempted, rolled back (InvestigationSession → Case, Auth Session → Auth)

**Target Completion:** Agent module extraction will bring Platform Evolution to **86% complete** (6/7 modules).

---

## Component Inventory

### 1. Core Services (4 files, ~2,348 LOC)

**Primary Services:**
1. **`services/agent_orchestration_service.py`** (1,068 lines)
   - Core agent execution orchestration
   - LLM streaming integration
   - Tool call coordination
   - Token budget tracking
   - Retry logic and error handling
   - **Dependencies**: LLM client, tool registry, case repository, session service

2. **`services/domain/investigation_orchestrator.py`** (911 lines)
   - Hypothesis-solution workflow orchestration
   - Confidence-based status transitions
   - Business rules enforcement
   - Investigation progress tracking
   - **Dependencies**: Case repository, hypothesis repository, agent orchestration

3. **`services/domain/investigation_service.py`** (369 lines)
   - Milestone-based troubleshooting workflow
   - Turn processing and OODA integration
   - Case retrieval and persistence
   - Progress tracking and reporting
   - **Dependencies**: Milestone engine, case repository, LLM provider

**Supporting Service:**
4. **`services/investigation_session_service.py`**
   - Session management (referenced by agent orchestration)
   - Budget tracking integration
   - **NOTE**: May belong to Session module if re-extracted

### 2. Domain Models (3 files, ~1,563 LOC)

1. **`models/agent_execution.py`** (318 lines)
   - `AgentExecution` dataclass - execution state and metadata
   - `AgentToolCall` dataclass - tool invocation records
   - `ExecutionStatus` enum - PENDING, RUNNING, COMPLETED, FAILED, CANCELLED
   - `AgentType` enum - INVESTIGATOR, DEBUGGER, RESEARCHER, VALIDATOR, REPORTER

2. **`models/agentic.py`** (1,245 lines)
   - **7-component agentic framework:**
     - `AgentExecutionState` - agent state management
     - `ConversationMemory` - conversation history
     - `ExecutionPlan`, `PlanNode` - execution planning
     - `ObservationData`, `AdaptationEvent` - learning
   - **Query classification** - 16 intent types
   - **Guardrails and policy models** - safety constraints
   - **Response synthesis models** - output formatting

3. **`models/investigation.py`**
   - Investigation state models
   - OODA-related models (Observe, Orient, Decide, Act)
   - Phase transition models

### 3. API Routes (1 file, 474 LOC)

**`api/routes/agent.py`** (474 lines)
- `POST /api/v1/cases/{case_id}/sessions/{session_id}/execute` - Execute agent
- `GET /api/v1/cases/{case_id}/sessions/{session_id}/executions` - List executions
- `GET /api/v1/cases/{case_id}/sessions/{session_id}/executions/{id}` - Get execution
- `POST /api/v1/cases/{case_id}/sessions/{session_id}/executions/{id}/cancel` - Cancel execution
- SSE streaming support for real-time updates
- Non-streaming mode for batch execution
- **Dependencies**: Agent orchestration service, session service, case service

### 4. Infrastructure (3 files)

**Repositories:**
1. **`infrastructure/persistence/agent_execution_repository.py`** (36,574 bytes)
   - Agent execution CRUD operations
   - Tool call persistence
   - Query methods for executions by case/session
   - Execution history tracking

2. **`infrastructure/persistence/investigation_session_repository.py`** (21,686 bytes)
   - Session persistence (used by agent orchestration)
   - Budget tracking
   - **NOTE**: May belong to Session module

**Domain Events:**
3. **`domain/events.py`**
   - `ExecutionEvent` and `ExecutionEventType` - agent execution events
   - `LLMEvent` and `LLMEventType` - LLM streaming events
   - `AgentContext`, `Message`, `Tool`, `ToolCall`, `ToolResult`
   - Event factory methods for streaming
   - **NOTE**: Contains both Agent-specific and shared event types

### 5. Tool System (13 files, ~2,000 LOC)

**Tool Infrastructure (2 files):**
1. **`tools/agent_tools.py`** (286 lines)
   - `AgentTool` base class - tool interface
   - `AgentToolRegistry` - tool registration and discovery
   - `ToolContext` - execution context with case/session data
   - Tool validation and JSON schema generation

2. **`tools/registry.py`**
   - Global tool registry
   - Tool lifecycle management

**Tool Implementations (9 files):**
1. **`tools/list_evidence_tool.py`** - List evidence files in case
2. **`tools/read_file_tool.py`** - Read evidence file contents
3. **`tools/knowledge_base.py`** - Global knowledge base search
4. **`tools/user_kb_qa.py`** - User knowledge base Q&A
5. **`tools/global_kb_qa.py`** - Global KB Q&A
6. **`tools/case_evidence_qa.py`** - Case evidence Q&A
7. **`tools/document_qa_tool.py`** - Document Q&A
8. **`tools/web_search.py`** - Web search tool
9. **`tools/kb_configs/`** - Tool configuration (3 files)
   - `user_kb_config.py`
   - `global_kb_config.py`
   - `case_evidence_config.py`

**Tool Dependencies:**
- Evidence tools → Evidence module
- Knowledge tools → Knowledge module
- Web search → External services

### 6. Core Investigation Components (7 files, ~2,000 LOC)

**Critical Decision:** These components are **shared infrastructure** used across investigation workflows. They may need to remain as horizontal infrastructure or be moved to a shared "Investigation Core" library.

1. **`core/investigation/investigation_coordinator.py`** (195 lines)
   - Multi-system conflict resolution
   - Anchoring prevention
   - Phase completion coordination

2. **`core/investigation/milestone_engine.py`**
   - Milestone-based workflow engine
   - Turn processing
   - LLM integration for milestone assessment

3. **`core/investigation/hypothesis_manager.py`**
   - Hypothesis lifecycle management
   - Confidence tracking and updates
   - Hypothesis validation

4. **`core/investigation/ooda_engine.py`**
   - OODA loop implementation (Observe, Orient, Decide, Act)
   - Cognitive bias prevention
   - Adaptive decision-making

5. **`core/investigation/phases.py`**
   - Investigation phase definitions
   - Phase transition logic
   - State machine for investigation progress

6. **`core/investigation/strategy_selector.py`**
   - Investigation strategy selection
   - Adaptive approach selection based on case characteristics

7. **`core/investigation/working_conclusion_generator.py`**
   - Working conclusion generation
   - Progress assessment
   - Interim result synthesis

### 7. Test Files (49+ files, ~5,000-7,000 LOC)

**Unit Tests (15+ files):**
- `tests/unit/services/test_agent_orchestration_service.py`
- `tests/unit/services/domain/test_investigation_orchestrator.py`
- `tests/unit/models/test_agent_execution.py`
- `tests/unit/api/test_agent_api_streaming.py`
- `tests/unit/api/test_agent_models.py`
- `tests/unit/tools/test_read_file_tool.py`
- `tests/unit/tools/test_tool_registry.py`
- `tests/unit/infrastructure/persistence/test_agent_execution_repository.py`
- Plus 7+ more unit test files

**Integration Tests (11+ files):**
- `tests/integration/test_agent_execution_integration.py`
- `tests/integration/test_agent_orchestration_integration.py`
- `tests/integration/test_agent_api_integration.py`
- `tests/integration/test_case_agent_end_to_end.py`
- `tests/integration/test_agentic_agent_service.py`
- `tests/integration/test_investigation_session_service_integration.py`
- `tests/integration/api/test_agent_api.py`
- Plus 4+ more integration test files

**Performance/Benchmark Tests (5+ files):**
- `tests/benchmarks/test_agent_orchestration_performance.py`
- `tests/benchmarks/test_agent_execution_operations.py`
- `tests/performance/test_case_agent_performance.py`

**Security Tests (1+ file):**
- `tests/security/test_case_agent_security_integration.py`

---

## Dependency Analysis

### Dependencies (What Agent Module Uses)

#### 1. Case Module (STRONG coupling)
**Usage:**
- Case retrieval for agent context
- Case status updates during investigation
- Case metadata for investigation planning

**Files:**
- `CaseRepository` - case data access
- `Case` model - case domain model
- Case status enums

**Integration Points:**
- Agent reads case data
- Agent updates case investigation status
- Agent creates investigation sessions linked to cases

#### 2. Evidence Module (STRONG coupling)
**Usage:**
- Evidence file listing in tools
- Evidence file reading for analysis
- Evidence artifact metadata

**Files:**
- `EvidenceArtifactService` - evidence access
- Evidence models
- Evidence search

**Integration Points:**
- `list_evidence_tool.py` - lists evidence files
- `read_file_tool.py` - reads evidence contents
- `case_evidence_qa.py` - Q&A over case evidence

#### 3. Knowledge Module (MEDIUM coupling)
**Usage:**
- Knowledge base search in tools
- Vector search for similar issues
- KB configuration

**Files:**
- Knowledge base search services
- Vector store integration
- KB configuration models

**Integration Points:**
- `knowledge_base.py` - global KB search
- `user_kb_qa.py` - user KB Q&A
- `global_kb_qa.py` - global KB Q&A

#### 4. Session Module (STRONG coupling - if re-extracted)
**Usage:**
- Investigation session management
- Session budget tracking
- Session lifecycle

**Files:**
- `InvestigationSessionService` - session management
- `InvestigationSession` model
- Session repository

**Integration Points:**
- Agent creates/updates investigation sessions
- Budget tracking during agent execution
- Session status transitions

#### 5. LLM Infrastructure (CRITICAL coupling)
**Usage:**
- LLM provider abstraction
- Streaming support
- Token counting
- Provider failover

**Files:**
- `integrations/llm_client.py` - LLM client wrapper
- `infrastructure/llm/providers/` - provider implementations
- Streaming utilities

**Integration Points:**
- Agent orchestration uses LLM for responses
- Tool calls processed through LLM
- Streaming events for real-time updates

#### 6. Shared Infrastructure (MEDIUM coupling)
**Usage:**
- Database persistence (PostgreSQL)
- Logging and observability
- Exception handling
- Authentication/authorization

**Files:**
- Repository base classes
- Logging utilities
- Exception classes
- Auth middleware

---

## Architectural Challenges

### Critical Challenges

#### 1. Three Overlapping Investigation Services ⚠️

**Problem:**
Three services handle investigation workflows with unclear boundaries:

- **`AgentOrchestrationService`** (1,068 LOC)
  - Executes agent with LLM integration
  - Manages tool calls
  - Handles streaming

- **`InvestigationOrchestrator`** (911 LOC)
  - Orchestrates hypothesis-solution workflow
  - Manages confidence-based transitions
  - Enforces business rules

- **`InvestigationService`** (369 LOC)
  - Milestone-based troubleshooting
  - Turn processing with OODA
  - Progress tracking

**Overlap:**
- All three coordinate investigation workflows
- All three interact with Case and Session modules
- Unclear which service owns which responsibility

**Resolution Options:**

**Option A: Merge into Single Service**
- Combine all three into `AgentInvestigationService`
- Clear single responsibility
- Easier to maintain
- **Risk**: Large service, potential god object

**Option B: Clear Separation of Concerns**
- `AgentOrchestrationService` - Low-level agent execution (LLM, tools, streaming)
- `InvestigationOrchestrator` - Mid-level workflow orchestration (hypothesis, phases)
- `InvestigationService` - High-level investigation management (milestones, OODA)
- **Risk**: Complex interaction patterns

**Option C: Extract Common Base**
- Create `BaseInvestigationService` with shared logic
- Each service handles specific workflow type
- **Risk**: Inheritance complexity

**Recommendation:** Start with **Option B** and refactor to **Option A** if complexity becomes unmanageable.

---

#### 2. Shared Core Investigation Components ⚠️

**Problem:**
`core/investigation/` contains 7 files (~2,000 LOC) that are foundational investigation infrastructure:
- OODA engine
- Milestone engine
- Phase definitions
- Hypothesis manager
- Investigation coordinator
- Strategy selector
- Working conclusion generator

**Question:** Do these belong in the Agent module or as shared infrastructure?

**Analysis:**

**If in Agent Module:**
- ✅ Clear ownership
- ✅ Vertical slice completeness
- ❌ Other modules can't use investigation patterns
- ❌ Tight coupling if other modules need investigation logic

**If Shared Infrastructure:**
- ✅ Reusable across modules
- ✅ Horizontal infrastructure pattern
- ❌ No clear ownership
- ❌ Breaks vertical slice purity

**Recommendation:**
Create a **shared Investigation Core library** (`faultmaven.investigation.core/`) that is:
- Owned by the Agent module team
- Provides investigation patterns and engines
- Used by Agent module as primary consumer
- Available to other modules if needed

**Structure:**
```
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
```

---

#### 3. Tool System Spanning Multiple Modules ⚠️

**Problem:**
The tool system has 9 tools with dependencies across multiple modules:

**Evidence Tools:**
- `list_evidence_tool.py` → Evidence module
- `read_file_tool.py` → Evidence module
- `case_evidence_qa.py` → Evidence module

**Knowledge Tools:**
- `knowledge_base.py` → Knowledge module
- `user_kb_qa.py` → Knowledge module
- `global_kb_qa.py` → Knowledge module
- `document_qa_tool.py` → Knowledge module

**Web Tools:**
- `web_search.py` → External services

**Question:** Where should tools live?

**Resolution Options:**

**Option A: All Tools in Agent Module**
- Agent module owns entire tool system
- Tools import from other modules (Evidence, Knowledge)
- **Pro**: Clear ownership, single registry
- **Con**: Agent module depends on Evidence + Knowledge

**Option B: Tools Stay in Source Modules**
- Evidence tools → Evidence module
- Knowledge tools → Knowledge module
- Web tools → Agent module (or shared utilities)
- Tool registry in Agent module discovers tools from all modules
- **Pro**: Clear module boundaries, loose coupling
- **Con**: Distributed tool system, complex registration

**Option C: Shared Tool Infrastructure**
- `tools/` becomes shared infrastructure
- Each module contributes tools
- Agent module consumes tools
- **Pro**: Flexible, extensible
- **Con**: No clear ownership

**Recommendation:** **Option B** - Tools in source modules with Agent-owned registry
- Evidence module provides evidence tools
- Knowledge module provides knowledge tools
- Agent module provides agent-specific tools (web search)
- `AgentToolRegistry` discovers tools via plugin pattern

---

#### 4. LLM Infrastructure Coupling ⚠️

**Problem:**
Agent orchestration is deeply tied to LLM client:
- Streaming implementation specific to LLM
- Token counting and budget tracking
- Provider failover logic
- Retry mechanisms

**Question:** Should LLM infrastructure be part of Agent module?

**Analysis:**

**Current State:**
- `integrations/llm_client.py` - LLM client wrapper
- `infrastructure/llm/providers/` - OpenAI, Anthropic, etc.
- Used by Agent orchestration service

**Options:**

**Option A: LLM in Agent Module**
- Agent module owns LLM integration
- Clear coupling
- **Pro**: Vertical slice completeness
- **Con**: Other modules can't use LLM (e.g., Report module for summaries)

**Option B: LLM as Shared Infrastructure**
- LLM client remains in `infrastructure/`
- Agent module uses LLM as dependency
- **Pro**: Reusable across modules
- **Con**: Agent module doesn't fully own its stack

**Recommendation:** **Option B** - LLM as shared infrastructure
- LLM infrastructure stays in `infrastructure/llm/`
- Agent module imports and uses LLM client
- Allows Report, Knowledge, or other modules to use LLM if needed
- Maintain clear interface boundaries

---

#### 5. Session Management Coupling ⚠️

**Problem:**
Agent orchestration requires session management:
- `InvestigationSessionService` manages sessions
- Budget tracking per session
- Session lifecycle tied to agent execution

**Question:** Should `InvestigationSessionService` be in Agent or Session module?

**Context:**
- Session module was previously extracted and rolled back
- `InvestigationSession` moved to Case module
- Auth sessions moved to Auth module

**Recommendation:**
- `InvestigationSessionService` → **Agent module**
- `InvestigationSession` model stays in Case module (it has `case_id`)
- Agent module imports `InvestigationSession` from Case module
- Clear one-way dependency: Agent → Case

**Rationale:**
- Session service is primarily used by agent orchestration
- No other module needs investigation session management
- Avoids re-creating Session module

---

### Medium Challenges

#### 6. Domain Events Shared Across Modules

**Problem:**
`domain/events.py` contains both agent-specific and generic events:
- `ExecutionEvent` - Agent-specific
- `LLMEvent` - Could be shared
- `AgentContext`, `Message`, `Tool` - Agent-specific

**Resolution:**
Split into:
- `modules/agent/domain/events/execution_events.py` - Agent-specific events
- `infrastructure/events/llm_events.py` - Shared LLM events (if needed)

---

#### 7. Repository Pattern Consistency

**Challenge:** Ensure Agent repositories follow established patterns from other modules.

**Solution:**
- Follow Evidence/Knowledge/Case repository patterns
- Use SQLAlchemy models
- Implement query methods
- Add proper error handling

---

#### 8. API Route Organization

**Challenge:** Agent routes nested under cases/sessions path creates dependency on Case/Session routing.

**Current:**
```
POST /api/v1/cases/{case_id}/sessions/{session_id}/execute
```

**Options:**

**Option A: Keep Nested Routes**
- Maintains RESTful hierarchy
- Clear context (case → session → execution)
- **Con**: Depends on Case/Session routing structure

**Option B: Flat Agent Routes**
- `/api/v1/agent/execute?case_id={id}&session_id={id}`
- Agent module owns routing
- **Con**: Breaks REST conventions

**Recommendation:** **Option A** - Keep nested routes
- Agent API routes can still be in Agent module
- Routes reference case_id and session_id as parameters
- Maintains RESTful design

---

### Minor Challenges

#### 9. Test Isolation

**Challenge:** Integration tests span multiple modules (Case → Session → Agent → Evidence).

**Solution:**
- Keep integration tests in separate `tests/integration/` directory
- Mock module boundaries for unit tests
- Maintain end-to-end test suite

---

#### 10. Configuration & Dependency Injection

**Challenge:** Agent services registered in central DI container. Tool registry is global singleton.

**Solution:**
- Agent module provides its own DI configuration
- Tool registry pattern:
  - Each module registers tools with central registry
  - Agent module initializes and owns registry
  - Plugin-based discovery

---

## Recommended Module Structure

Based on the architectural analysis, here's the recommended structure for the Agent module:

```
faultmaven/modules/agent/
├── __init__.py
├── api/
│   ├── __init__.py
│   ├── routes.py              # Agent execution endpoints
│   ├── dependencies.py        # FastAPI dependencies
│   └── streaming.py           # SSE streaming utilities
├── domain/
│   ├── __init__.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── agent_execution.py  # AgentExecution, AgentToolCall
│   │   ├── agentic.py          # Agentic framework models
│   │   └── investigation.py    # Investigation state models
│   ├── events/
│   │   ├── __init__.py
│   │   └── execution_events.py # Agent execution events
│   └── services/
│       ├── __init__.py
│       ├── agent_orchestration_service.py
│       ├── investigation_orchestrator.py
│       ├── investigation_service.py
│       └── investigation_session_service.py
├── infrastructure/
│   ├── __init__.py
│   ├── persistence/
│   │   ├── __init__.py
│   │   └── agent_execution_repository.py
│   └── tools/
│       ├── __init__.py
│       ├── base.py             # AgentTool, ToolContext
│       ├── registry.py         # ToolRegistry
│       └── web_search_tool.py  # Agent-specific tools
└── README.md

# Shared Investigation Core (separate from Agent module)
faultmaven/investigation/
├── __init__.py
├── core/
│   ├── __init__.py
│   ├── engines/
│   │   ├── __init__.py
│   │   ├── ooda_engine.py
│   │   ├── milestone_engine.py
│   │   └── hypothesis_engine.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── phases.py
│   │   ├── strategies.py
│   │   └── conclusions.py
│   └── coordinators/
│       ├── __init__.py
│       └── investigation_coordinator.py
└── README.md

# Tools Distributed Across Modules
faultmaven/modules/evidence/tools/
├── list_evidence_tool.py
├── read_file_tool.py
└── case_evidence_qa_tool.py

faultmaven/modules/knowledge/tools/
├── knowledge_base_tool.py
├── user_kb_qa_tool.py
├── global_kb_qa_tool.py
└── document_qa_tool.py
```

**Key Design Decisions:**
1. **Agent module** owns orchestration, execution, and agent-specific logic
2. **Investigation Core** is shared infrastructure owned by Agent team
3. **Tools** distributed to source modules (Evidence, Knowledge) with registry in Agent
4. **LLM infrastructure** remains shared in `infrastructure/llm/`
5. **InvestigationSessionService** in Agent module, `InvestigationSession` model in Case module

---

## Phased Implementation Roadmap

### Phase 1: Preparation & Design (Week 1)

**Goals:**
- Finalize architectural decisions
- Create shared Investigation Core library
- Document module interfaces and contracts
- Set up module boundary tests

**Tasks:**
1. Review and approve architectural decisions in this document
2. Create `faultmaven/investigation/` directory structure
3. Move core investigation components to Investigation Core:
   - `core/investigation/ooda_engine.py` → `investigation/core/engines/ooda_engine.py`
   - `core/investigation/milestone_engine.py` → `investigation/core/engines/milestone_engine.py`
   - `core/investigation/hypothesis_manager.py` → `investigation/core/engines/hypothesis_engine.py`
   - `core/investigation/phases.py` → `investigation/core/models/phases.py`
   - `core/investigation/strategy_selector.py` → `investigation/core/models/strategies.py`
   - `core/investigation/working_conclusion_generator.py` → `investigation/core/models/conclusions.py`
   - `core/investigation/investigation_coordinator.py` → `investigation/core/coordinators/investigation_coordinator.py`
4. Update imports to use Investigation Core
5. Define module interface contracts:
   - Agent → Case: `Case` model, `CaseRepository`
   - Agent → Evidence: Evidence tools
   - Agent → Knowledge: Knowledge tools
   - Agent → Session: `InvestigationSession` model
6. Create module boundary tests
7. **Deliverable:** Investigation Core library with passing tests

---

### Phase 2: Infrastructure & Events (Week 2)

**Goals:**
- Extract Agent infrastructure components
- Set up domain events
- Configure module-specific DI

**Tasks:**
1. Create `modules/agent/infrastructure/` structure
2. Move agent execution repository:
   - `infrastructure/persistence/agent_execution_repository.py` → `modules/agent/infrastructure/persistence/agent_execution_repository.py`
3. Extract agent-specific domain events:
   - Create `modules/agent/domain/events/execution_events.py`
   - Move `ExecutionEvent`, `ExecutionEventType` from `domain/events.py`
   - Leave shared LLM events in `infrastructure/events/` (if needed)
4. Move investigation session repository to Agent module:
   - `infrastructure/persistence/investigation_session_repository.py` → `modules/agent/infrastructure/persistence/investigation_session_repository.py`
5. Set up Agent module DI configuration:
   - Create `modules/agent/di/` for dependency injection
   - Register Agent services
   - Configure tool registry
6. Update container to use Agent module DI
7. **Deliverable:** Agent infrastructure with passing repository tests

---

### Phase 3: Domain Models & Services (Week 3)

**Goals:**
- Extract Agent domain models
- Extract and clarify Agent services
- Resolve service overlap

**Tasks:**
1. Create `modules/agent/domain/models/` structure
2. Move agent models:
   - `models/agent_execution.py` → `modules/agent/domain/models/agent_execution.py`
   - `models/agentic.py` → `modules/agent/domain/models/agentic.py`
   - `models/investigation.py` → `modules/agent/domain/models/investigation.py`
3. Create `modules/agent/domain/services/` structure
4. Clarify service responsibilities (see Challenge #1):
   - **`AgentOrchestrationService`** - Low-level agent execution
   - **`InvestigationOrchestrator`** - Mid-level workflow orchestration
   - **`InvestigationService`** - High-level investigation management
5. Move services:
   - `services/agent_orchestration_service.py` → `modules/agent/domain/services/agent_orchestration_service.py`
   - `services/domain/investigation_orchestrator.py` → `modules/agent/domain/services/investigation_orchestrator.py`
   - `services/domain/investigation_service.py` → `modules/agent/domain/services/investigation_service.py`
   - `services/investigation_session_service.py` → `modules/agent/domain/services/investigation_session_service.py`
6. Update service interactions and dependencies
7. Update imports across codebase
8. **Deliverable:** Agent services with passing unit tests

---

### Phase 4: Tool System & API (Week 4)

**Goals:**
- Implement distributed tool pattern
- Extract Agent API routes
- Set up tool registry

**Tasks:**
1. Create tool infrastructure in Agent module:
   - `modules/agent/infrastructure/tools/base.py` - `AgentTool` base class
   - `modules/agent/infrastructure/tools/registry.py` - `AgentToolRegistry`
2. Move agent-specific tools:
   - `tools/web_search.py` → `modules/agent/infrastructure/tools/web_search_tool.py`
3. Create tool directories in source modules:
   - `modules/evidence/tools/`
   - `modules/knowledge/tools/`
4. Move evidence tools to Evidence module:
   - `tools/list_evidence_tool.py` → `modules/evidence/tools/list_evidence_tool.py`
   - `tools/read_file_tool.py` → `modules/evidence/tools/read_file_tool.py`
   - `tools/case_evidence_qa.py` → `modules/evidence/tools/case_evidence_qa_tool.py`
5. Move knowledge tools to Knowledge module:
   - `tools/knowledge_base.py` → `modules/knowledge/tools/knowledge_base_tool.py`
   - `tools/user_kb_qa.py` → `modules/knowledge/tools/user_kb_qa_tool.py`
   - `tools/global_kb_qa.py` → `modules/knowledge/tools/global_kb_qa_tool.py`
   - `tools/document_qa_tool.py` → `modules/knowledge/tools/document_qa_tool.py`
6. Implement plugin-based tool discovery:
   - Each module registers tools with central registry
   - Registry discovers tools at startup
7. Create `modules/agent/api/` structure
8. Move agent API routes:
   - `api/routes/agent.py` → `modules/agent/api/routes.py`
9. Create `modules/agent/api/streaming.py` for SSE utilities
10. Update API routing to include Agent module routes
11. Update import paths across codebase
12. **Deliverable:** Agent API with passing integration tests, working tool system

---

### Phase 5: Testing, Integration & Finalization (Week 5)

**Goals:**
- Move and adapt test suite
- Integration testing across modules
- Performance and security testing
- Documentation and cleanup

**Tasks:**
1. Create test structure:
   - `tests/modules/agent/unit/`
   - `tests/modules/agent/integration/`
2. Move unit tests:
   - Move 15+ unit test files to `tests/modules/agent/unit/`
   - Update import paths
   - Fix any broken tests
3. Move integration tests:
   - Move 11+ integration test files to `tests/modules/agent/integration/`
   - Ensure cross-module integration works
4. Move performance tests:
   - Move benchmark tests to `tests/modules/agent/benchmarks/`
   - Verify performance metrics
5. Move security tests:
   - Move security test to `tests/modules/agent/security/`
6. Run full test suite:
   - All unit tests (target: 100% pass)
   - All integration tests (target: 95%+ pass)
   - Performance benchmarks
   - Security tests
7. Integration testing across modules:
   - Case → Agent workflow
   - Agent → Evidence tool calls
   - Agent → Knowledge tool calls
   - End-to-end case investigation flow
8. Update documentation:
   - Create `modules/agent/README.md`
   - Document Agent module architecture
   - Update main Platform Evolution documentation
   - Create migration guide for developers
9. Clean up old locations:
   - Remove `services/agent_orchestration_service.py`
   - Remove `services/domain/investigation_*.py`
   - Remove `models/agent_execution.py`, `models/agentic.py`
   - Remove `api/routes/agent.py`
   - Remove `infrastructure/persistence/agent_execution_repository.py`
   - Update `services/__init__.py` to remove agent imports
10. Final verification:
    - All imports updated
    - No circular dependencies
    - All tests passing
    - Documentation complete
11. **Deliverable:** Fully extracted Agent module with comprehensive test coverage

---

## Import Update Strategy

### Files Importing Agent Services (~30-40 files)

Based on the analysis, approximately **30-40 files** will need import updates:

**Container/DI (5 files):**
- `container/providers/services.py`
- `container/base.py`
- `services/service_factory.py`
- `core/service_factories.py`
- Module-specific DI configs

**API Routes (7 files):**
- `api/routes/agent.py` (will move)
- `api/v1/routes/messages.py`
- `api/v1/routes/hypotheses.py`
- `api/routes/__init__.py`
- `api/dependencies.py`
- `api/v1/dependencies.py`
- Main app routing

**Services (5 files):**
- `services/__init__.py`
- `services/domain/__init__.py`
- `services/domain/case_service.py` (if it uses agent)
- Other services that invoke agent

**Models (3 files):**
- `models/__init__.py`
- `models/investigation.py` (if it imports agent models)
- Other models with agent dependencies

**Tests (15-20 files):**
- All agent-related test files
- Integration tests
- Conftest files

### Automated Import Update Script

Create `scripts/update_agent_imports.sh`:

```bash
#!/bin/bash
# Script to update all import references for Agent module extraction

set -e

echo "Updating agent module imports..."

# Service imports
find . -name "*.py" -type f -exec sed -i \
  -e 's|from faultmaven\.services\.agent_orchestration_service import|from faultmaven.modules.agent.domain.services.agent_orchestration_service import|g' \
  -e 's|from faultmaven\.services\.domain\.investigation_orchestrator import|from faultmaven.modules.agent.domain.services.investigation_orchestrator import|g' \
  -e 's|from faultmaven\.services\.domain\.investigation_service import|from faultmaven.modules.agent.domain.services.investigation_service import|g' \
  -e 's|from faultmaven\.services\.investigation_session_service import|from faultmaven.modules.agent.domain.services.investigation_session_service import|g' \
  {} +

# Model imports
find . -name "*.py" -type f -exec sed -i \
  -e 's|from faultmaven\.models\.agent_execution import|from faultmaven.modules.agent.domain.models.agent_execution import|g' \
  -e 's|from faultmaven\.models\.agentic import|from faultmaven.modules.agent.domain.models.agentic import|g' \
  -e 's|from faultmaven\.models\.investigation import|from faultmaven.modules.agent.domain.models.investigation import|g' \
  {} +

# Infrastructure imports
find . -name "*.py" -type f -exec sed -i \
  -e 's|from faultmaven\.infrastructure\.persistence\.agent_execution_repository import|from faultmaven.modules.agent.infrastructure.persistence.agent_execution_repository import|g' \
  {} +

# API route imports
find . -name "*.py" -type f -exec sed -i \
  -e 's|from faultmaven\.api\.routes\.agent import|from faultmaven.modules.agent.api.routes import|g' \
  {} +

# Tool imports
find . -name "*.py" -type f -exec sed -i \
  -e 's|from faultmaven\.tools\.agent_tools import|from faultmaven.modules.agent.infrastructure.tools.base import|g' \
  {} +

# Investigation Core imports (new library)
find . -name "*.py" -type f -exec sed -i \
  -e 's|from faultmaven\.core\.investigation\.ooda_engine import|from faultmaven.investigation.core.engines.ooda_engine import|g' \
  -e 's|from faultmaven\.core\.investigation\.milestone_engine import|from faultmaven.investigation.core.engines.milestone_engine import|g' \
  -e 's|from faultmaven\.core\.investigation\.hypothesis_manager import|from faultmaven.investigation.core.engines.hypothesis_engine import|g' \
  {} +

echo "✓ All import updates complete!"
```

---

## Testing Strategy

### Test Categories

1. **Unit Tests** - Test individual components in isolation
   - Service logic tests
   - Model validation tests
   - Repository query tests
   - Tool execution tests

2. **Integration Tests** - Test module interactions
   - Agent → Case integration
   - Agent → Evidence tool integration
   - Agent → Knowledge tool integration
   - End-to-end investigation flow

3. **Performance Tests** - Validate performance characteristics
   - Agent execution latency
   - Tool call overhead
   - Streaming performance
   - Concurrent execution capacity

4. **Security Tests** - Ensure security boundaries
   - Tool execution sandboxing
   - Input validation
   - Authorization checks
   - Data isolation

### Test Coverage Targets

- **Unit Tests**: 85%+ coverage
- **Integration Tests**: All critical workflows covered
- **Performance Tests**: Baseline metrics established
- **Security Tests**: All OWASP Top 10 scenarios covered

### Test Migration Checklist

- [ ] Move unit test files to `tests/modules/agent/unit/`
- [ ] Update test imports
- [ ] Fix broken tests due to module changes
- [ ] Move integration tests to `tests/modules/agent/integration/`
- [ ] Ensure cross-module integration tests pass
- [ ] Move performance tests to `tests/modules/agent/benchmarks/`
- [ ] Move security tests to `tests/modules/agent/security/`
- [ ] Verify all tests pass with new module structure
- [ ] Update CI/CD pipeline for Agent module tests

---

## Risk Mitigation

### High-Risk Areas

1. **Service Overlap Clarification**
   - **Risk**: Refactoring three services breaks existing workflows
   - **Mitigation**:
     - Phase 3 focuses exclusively on services
     - Comprehensive unit tests before/after
     - Integration tests validate workflows
     - Consider incremental refactoring (don't merge services immediately)

2. **LLM Integration Stability**
   - **Risk**: Breaking LLM streaming or provider integration
   - **Mitigation**:
     - Keep LLM infrastructure as shared (don't move)
     - Test streaming thoroughly
     - Validate all provider integrations (OpenAI, Anthropic, etc.)

3. **Tool System Distributed Pattern**
   - **Risk**: Tool discovery fails, tools don't register correctly
   - **Mitigation**:
     - Implement tool registry with comprehensive tests
     - Use plugin pattern with clear interfaces
     - Fallback to direct imports if discovery fails
     - Test each tool individually after move

4. **Investigation Core Extraction**
   - **Risk**: Breaking investigation workflows used across modules
   - **Mitigation**:
     - Phase 1 focuses on Investigation Core
     - Maintain backward compatibility during transition
     - Update all imports systematically
     - Test all investigation workflows

5. **Circular Dependencies**
   - **Risk**: Agent → Case → Agent circular dependency
   - **Mitigation**:
     - Enforce one-way dependencies: Agent → Case (not bidirectional)
     - Use events for Case → Agent communication if needed
     - Module boundary tests to catch circular imports

### Rollback Plan

If Agent module extraction fails or creates critical issues:

1. **Rollback Commits**: Use `git revert` or `git reset` to rollback changes
2. **Restore Imports**: Run reverse import script to restore old paths
3. **Re-run Tests**: Verify system works after rollback
4. **Document Issues**: Create detailed report of failure points
5. **Re-plan**: Adjust extraction plan based on lessons learned

---

## Success Criteria

### Functional Criteria

- [ ] All Agent services successfully extracted and working
- [ ] All Agent models extracted with passing validation
- [ ] Agent API routes functional with streaming support
- [ ] Tool system working with distributed pattern
- [ ] Investigation Core library extracted and functional
- [ ] All integration flows working (Case → Agent → Evidence/Knowledge)

### Technical Criteria

- [ ] Zero circular dependencies
- [ ] All imports updated (no references to old locations)
- [ ] Module structure follows established pattern
- [ ] Clear module boundaries defined
- [ ] 85%+ test coverage maintained
- [ ] No performance regressions (< 5% slowdown acceptable)

### Quality Criteria

- [ ] All unit tests passing (100%)
- [ ] All integration tests passing (95%+)
- [ ] Performance benchmarks meeting targets
- [ ] Security tests passing
- [ ] Code review approved
- [ ] Documentation complete

### Documentation Criteria

- [ ] Agent module README created
- [ ] Architecture decisions documented
- [ ] API documentation updated
- [ ] Migration guide for developers
- [ ] Platform Evolution status updated (71% → 86%)

---

## Post-Extraction Cleanup

After Agent module extraction is complete:

1. **Remove Old Files**:
   - Delete `services/agent_orchestration_service.py`
   - Delete `services/domain/investigation_orchestrator.py`
   - Delete `services/domain/investigation_service.py`
   - Delete `models/agent_execution.py`
   - Delete `models/agentic.py`
   - Delete `api/routes/agent.py`
   - Delete old tool files from `tools/`

2. **Update Central Imports**:
   - Remove agent imports from `services/__init__.py`
   - Remove agent imports from `models/__init__.py`
   - Update API routing in `api/__init__.py`

3. **Archive Analysis Documents**:
   - Move working documents to `docs/archive/2026/01/`
   - Keep module extraction plans as reference

4. **Update Platform Documentation**:
   - Update main README with new module count
   - Update architecture diagrams
   - Celebrate Platform Evolution completion! 🎉

---

## Timeline & Effort Estimate

### Total Effort: 3-5 Weeks (Full-Time)

**Phase 1: Preparation** - 1 week
- Investigation Core extraction
- Interface design
- Module boundary setup

**Phase 2: Infrastructure** - 1 week
- Repositories
- Events
- DI configuration

**Phase 3: Services & Models** - 1 week
- Service clarification
- Model extraction
- Service extraction

**Phase 4: Tools & API** - 1 week
- Tool system refactoring
- API route extraction
- Tool registry implementation

**Phase 5: Testing & Finalization** - 1 week
- Test migration
- Integration testing
- Documentation
- Cleanup

### Resource Requirements

- **Developer**: 1 senior developer with platform architecture knowledge
- **Reviewer**: 1-2 reviewers for code review
- **Tester**: QA support for integration and performance testing
- **Documentation**: Technical writer for comprehensive docs

---

## Next Steps

### Immediate Actions (Before Starting Extraction)

1. **Review This Plan**
   - Team review of architectural decisions
   - Approve/adjust extraction strategy
   - Confirm phased timeline

2. **Stakeholder Alignment**
   - Product team: Impact on roadmap
   - Engineering team: Resource allocation
   - QA team: Testing requirements

3. **Environment Preparation**
   - Create feature branch for extraction
   - Set up CI/CD for Agent module
   - Prepare test environments

4. **Kick-off Phase 1**
   - Schedule Phase 1 work
   - Assign resources
   - Begin Investigation Core extraction

### Long-Term Vision

**After Agent Module Extraction:**
- **Platform Evolution: 86% Complete** (6/7 modules)
- **Remaining**: Session module re-evaluation (optional)
- **Next**: Microservices consideration (if needed)
- **Future**: API gateway, event-driven architecture, service mesh

---

## References

- [Auth Module Extraction Complete](./AUTH-MODULE-EXTRACTION-COMPLETE.md)
- [Platform Evolution Checkpoint](./CHECKPOINT-2026-01-06.md)
- [Module Extraction Decisions](./MODULE-EXTRACTION-DECISIONS.md)
- [Session Deferral Decision](./SESSION-DEFERRAL-DECISION.md)

---

## Appendix A: File Listing

### Production Files to Move (35-40 files)

**Services (4 files):**
1. `services/agent_orchestration_service.py`
2. `services/domain/investigation_orchestrator.py`
3. `services/domain/investigation_service.py`
4. `services/investigation_session_service.py`

**Models (3 files):**
5. `models/agent_execution.py`
6. `models/agentic.py`
7. `models/investigation.py`

**API (1 file):**
8. `api/routes/agent.py`

**Infrastructure (3 files):**
9. `infrastructure/persistence/agent_execution_repository.py`
10. `infrastructure/persistence/investigation_session_repository.py`
11. `domain/events.py` (partial - agent events only)

**Tools (13 files):**
12. `tools/agent_tools.py`
13. `tools/registry.py`
14. `tools/list_evidence_tool.py`
15. `tools/read_file_tool.py`
16. `tools/knowledge_base.py`
17. `tools/user_kb_qa.py`
18. `tools/global_kb_qa.py`
19. `tools/case_evidence_qa.py`
20. `tools/document_qa_tool.py`
21. `tools/web_search.py`
22. `tools/kb_configs/user_kb_config.py`
23. `tools/kb_configs/global_kb_config.py`
24. `tools/kb_configs/case_evidence_config.py`

**Core Investigation (7 files):**
25. `core/investigation/investigation_coordinator.py`
26. `core/investigation/milestone_engine.py`
27. `core/investigation/hypothesis_manager.py`
28. `core/investigation/ooda_engine.py`
29. `core/investigation/phases.py`
30. `core/investigation/strategy_selector.py`
31. `core/investigation/working_conclusion_generator.py`

**Supporting (5+ files):**
32-36. Various utilities and helpers

### Test Files to Move (49 files)

**Unit Tests (15+ files)**
**Integration Tests (11+ files)**
**Performance Tests (5+ files)**
**Security Tests (1+ file)**

---

## Appendix B: Dependency Graph

```
Agent Module Dependencies:

Agent
├── Case (STRONG)
│   ├── Case model
│   ├── CaseRepository
│   └── InvestigationSession model
├── Evidence (STRONG)
│   ├── EvidenceArtifactService
│   ├── Evidence tools (list, read, qa)
│   └── Evidence models
├── Knowledge (MEDIUM)
│   ├── Knowledge base search
│   ├── Knowledge tools (kb, qa)
│   └── Vector search
├── LLM Infrastructure (CRITICAL)
│   ├── LLM client
│   ├── Streaming support
│   └── Provider abstraction
└── Shared Infrastructure (MEDIUM)
    ├── Persistence (PostgreSQL)
    ├── Logging/observability
    └── Authentication

Investigation Core (Shared Library)
└── Used by Agent module
    ├── OODA engine
    ├── Milestone engine
    ├── Hypothesis engine
    └── Phase management
```

---

**End of Agent Module Extraction Plan**

**Version**: 1.0
**Date**: 2026-01-06
**Status**: Ready for Review and Approval
**Next Action**: Team review and Phase 1 kickoff
