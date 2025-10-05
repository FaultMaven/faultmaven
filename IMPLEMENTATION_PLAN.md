# FaultMaven Implementation Plan
## Building a Well-Structured, Robust, Powerful, Scalable & Expandable System

**Document Version:** 1.0
**Date:** 2025-09-30
**Status:** Planning Phase
**Target Completion:** 8-10 weeks

---

## Executive Summary

This plan addresses critical gaps identified in the comprehensive architecture audit of FaultMaven. While the system has excellent foundational components (7-component agentic framework, multi-LLM support, privacy-first design), there is a significant disconnect between the documented architecture and actual implementation.

**Key Findings:**
- ✅ **Strengths**: Complete agentic framework (8,532 lines), robust infrastructure, 1425+ tests
- ❌ **Critical Gaps**: Memory system missing, agentic components not orchestrated, thin prompt engineering
- 🎯 **Goal**: Transform FaultMaven into a production-ready, intelligent troubleshooting copilot

**Expected Outcomes:**
- 📈 **Response Quality**: +50-70% improvement in relevancy, accuracy, and guidance
- 🏗️ **Architecture**: Fully integrated 7-component agentic system with proper orchestration
- 🧠 **Intelligence**: Context-aware memory system with learning capabilities
- 🛠️ **Extensibility**: Clean tool architecture for rapid capability expansion
- 📊 **Observability**: Complete tracing and metrics for production operations

---

## Table of Contents

1. [Current State Assessment](#current-state-assessment)
2. [Architecture Vision](#architecture-vision)
3. [Implementation Phases](#implementation-phases)
4. [Detailed Task Breakdown](#detailed-task-breakdown)
5. [Success Criteria](#success-criteria)
6. [Risk Mitigation](#risk-mitigation)
7. [Testing Strategy](#testing-strategy)
8. [Deployment Plan](#deployment-plan)

---

## Current State Assessment

### Architecture Audit Summary

**What's Working Well** ✅

1. **Agentic Framework Components** (Production Ready)
   - 7 complete components: State Manager, Classification Engine, Tool Broker, Guardrails, Response Synthesizer, Error Manager, Workflow Engine
   - 8,532 lines of sophisticated implementation
   - Full interface compliance with proper DI container integration

2. **Infrastructure Layer** (Robust)
   - Multi-LLM support (7 providers with intelligent routing)
   - PII redaction with Presidio integration
   - Redis-backed session management with client resumption
   - ChromaDB RAG knowledge base with BGE-M3 embeddings
   - Opik observability integration

3. **Clean Architecture** (Well-Structured)
   - Interface-based design with comprehensive DI container
   - Clear layer separation (API → Service → Agentic → Core → Infrastructure)
   - 1425+ tests across all architectural layers

**Critical Gaps** ❌

1. **Memory System Missing** (BLOCKER)
   - Exists only in backup directory
   - No conversation memory beyond current session
   - No pattern learning or context consolidation
   - **Impact**: Agent forgets context, repeats questions, no personalization

2. **Agentic Framework Not Orchestrated** (CRITICAL)
   - Components exist but AgentService bypasses them
   - Direct LLM calls instead of structured reasoning
   - Five-phase SRE doctrine not applied
   - **Impact**: Generic responses, no structured troubleshooting, poor tool usage

3. **Prompt Engineering Too Basic** (HIGH)
   - Simple system prompt without persona/methodology
   - No few-shot examples or output formatting
   - No tool usage instructions
   - **Impact**: -30-40% quality reduction

4. **Limited Tool Ecosystem** (HIGH)
   - Only 2 tools (KnowledgeBase, WebSearch)
   - No log analysis, metrics, config validation
   - **Impact**: Cannot perform actual troubleshooting tasks

5. **Context Management Simplistic** (MEDIUM)
   - Fixed 5-message window
   - No intelligent summarization
   - No token budget management
   - **Impact**: Loses important context in long conversations

6. **User-Scoped Knowledge Base Missing** (HIGH)
   - No user-specific knowledge base isolation
   - All users share same knowledge base
   - No personal/team-specific runbooks
   - **Impact**: Cannot provide personalized troubleshooting guidance

7. **User-Specific Memory Missing** (HIGH)
   - No user-scoped memory persistence
   - No learning from user's specific patterns
   - No personalization based on user's tech stack
   - **Impact**: Generic responses, no user-specific learning

8. **ViewState Implementation Incomplete** (MEDIUM)
   - Missing required API fields (user, cases, messages, memory_context, planning_state)
   - **Impact**: Frontend cannot render proper state

### Response Quality Impact Analysis

| Factor | Current State | Impact on Quality | Priority |
|--------|---------------|-------------------|----------|
| Memory System | Missing | ⭐⭐⭐⭐⭐ Massive | P0 |
| Agentic Orchestration | Not integrated | ⭐⭐⭐⭐⭐ Massive | P0 |
| Prompt Engineering | Basic | ⭐⭐⭐⭐ High | P0 |
| User-Scoped Knowledge Base | Missing | ⭐⭐⭐⭐ High | P1 |
| User-Specific Memory | Missing | ⭐⭐⭐⭐ High | P1 |
| Tool Ecosystem | Limited (2 tools) | ⭐⭐⭐⭐ High | P1 |
| Context Management | Simplistic | ⭐⭐⭐⭐ High | P1 |
| Knowledge Base Quality | Good foundation | ⭐⭐⭐⭐ High | P1 |
| ViewState | Incomplete | ⭐⭐⭐ Medium | P2 |
| Source Attribution | Minimal | ⭐⭐⭐ Medium | P2 |

---

## Architecture Vision

### Target Architecture: Fully Integrated Agentic System

```
┌─────────────────────────────────────────────────────────────────────┐
│                           Browser Extension                          │
│                    (React UI with TypeScript)                        │
└────────────────────────────────┬────────────────────────────────────┘
                                 │ HTTPS/WebSocket
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          API Layer (FastAPI)                         │
│  ┌─────────────────┬──────────────────┬──────────────────────────┐  │
│  │ /agent/query    │ /session/*       │ /knowledge/*             │  │
│  │ /case/*         │ /data/upload     │ Authentication           │  │
│  └─────────────────┴──────────────────┴──────────────────────────┘  │
└────────────────────────────────┬────────────────────────────────────┘
                                 │ Request Context Propagation
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Service Orchestration Layer                       │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                      AgentService                             │   │
│  │  • Query preprocessing & sanitization                        │   │
│  │  • Agentic framework orchestration                           │   │
│  │  • Response assembly & validation                            │   │
│  └──────────────────────────────────────────────────────────────┘   │
│  ┌──────────────────┬────────────────┬─────────────────────────┐   │
│  │ SessionService   │ CaseService    │ KnowledgeService        │   │
│  │ DataService      │ PlanningService│ UserKnowledgeService    │   │
│  │ UserMemoryService│ AnalyticsService│ UserProfileService     │   │
│  └──────────────────┴────────────────┴─────────────────────────┘   │
└────────────────────────────────┬────────────────────────────────────┘
                                 │ Orchestration
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│              7-Component Agentic Framework (INTEGRATED)              │
│                                                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  1. QueryClassificationEngine                                 │  │
│  │     • Intent classification (troubleshoot, status, explain)   │  │
│  │     • Complexity assessment (simple → expert)                 │  │
│  │     • Domain identification (DB, network, infra)              │  │
│  │     • Urgency detection (critical, high, medium, low)         │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              ▼                                        │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  2. AgentStateManager (MEMORY SYSTEM)                         │  │
│  │     • Short-term memory (current session)                     │  │
│  │     • Long-term memory (Redis persistence)                    │  │
│  │     • User-specific memory (personalized patterns)            │  │
│  │     • Pattern learning (user/system patterns)                 │  │
│  │     • Context consolidation (summarize long history)          │  │
│  │     • Strategic planning state                                │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              ▼                                        │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  3. ToolSkillBroker                                           │  │
│  │     • Dynamic tool selection based on classification          │  │
│  │     • Capability orchestration                                │  │
│  │     • Tool execution coordination                             │  │
│  │     • Result aggregation                                      │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              ▼                                        │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  4. BusinessLogicWorkflowEngine                               │  │
│  │     • Five-phase SRE doctrine implementation                  │  │
│  │     • Plan → Execute → Observe → Re-plan cycles               │  │
│  │     • Multi-turn reasoning with state transitions             │  │
│  │     • Autonomous decision-making                              │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              ▼                                        │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  5. GuardrailsPolicyLayer                                     │  │
│  │     • PII protection validation                               │  │
│  │     • Response safety checks                                  │  │
│  │     • Output validation                                       │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              ▼                                        │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  6. ResponseSynthesizer                                       │  │
│  │     • Multi-source information assembly                       │  │
│  │     • Quality validation                                      │  │
│  │     • Format standardization (markdown)                       │  │
│  │     • Source attribution                                      │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              ▼                                        │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  7. ErrorFallbackManager                                      │  │
│  │     • Intelligent error recovery                              │  │
│  │     • Circuit breaker patterns                                │  │
│  │     • Graceful degradation                                    │  │
│  └───────────────────────────────────────────────────────────────┘  │
└────────────────────────────────┬────────────────────────────────────┘
                                 │ Execution
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         Core Domain Logic                            │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  Five-Phase SRE Troubleshooting Doctrine                      │  │
│  │  1. Define Blast Radius → 2. Establish Timeline               │  │
│  │  3. Formulate Hypothesis → 4. Validate Hypothesis             │  │
│  │  5. Propose Solution                                          │  │
│  └───────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────┬────────────────┬─────────────────────────┐   │
│  │ Data Processing  │ Log Analysis   │ Knowledge Operations    │   │
│  └──────────────────┴────────────────┴─────────────────────────┘   │
└────────────────────────────────┬────────────────────────────────────┘
                                 │ Tool Invocation
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Tool Ecosystem (Expanded)                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ KnowledgeBaseTool    │ WebSearchTool      │ LogAnalysisTool  │   │
│  │ MetricsQueryTool     │ ConfigValidation   │ RunbookSearch    │   │
│  │ IncidentHistoryTool  │ CommandExecutor    │ DiagramGenerator │   │
│  └──────────────────────────────────────────────────────────────┘   │
└────────────────────────────────┬────────────────────────────────────┘
                                 │ Backend Calls
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Infrastructure Layer                            │
│  ┌──────────────────┬────────────────┬─────────────────────────┐   │
│  │ LLM Router       │ Redis Store    │ ChromaDB Vector Store   │   │
│  │ (7 Providers)    │ (Sessions)     │ (Global Knowledge Base) │   │
│  │                  │ (User Memory)  │ (User-Scoped KB)        │   │
│  └──────────────────┴────────────────┴─────────────────────────┘   │
│  ┌──────────────────┬────────────────┬─────────────────────────┐   │
│  │ Presidio PII     │ Opik Tracing   │ Prometheus Metrics      │   │
│  │ Redaction        │ (Observability)│ (Monitoring)            │   │
│  └──────────────────┴────────────────┴─────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### Key Architectural Principles

1. **Agentic-First Design**
   - All queries flow through the 7-component framework
   - No direct LLM calls (all mediated by workflow engine)
   - Plan→Execute→Observe→Re-plan cycles for complex queries

2. **Memory-Augmented Intelligence**
   - Persistent memory across sessions
   - User-specific memory for personalized learning
   - Pattern learning from troubleshooting history
   - Context-aware responses based on user's stack

3. **Tool-Driven Capabilities**
   - Extensible tool architecture
   - Dynamic tool selection based on query classification
   - Clean BaseTool interface for rapid expansion

4. **Structured Reasoning**
   - Five-phase SRE doctrine for all troubleshooting
   - Evidence-driven hypothesis validation
   - Multi-source information synthesis

5. **User-Scoped Personalization**
   - User-specific knowledge base isolation
   - Personalized memory and learning patterns
   - Customized responses based on user's tech stack
   - Team-specific runbooks and procedures

6. **Production-Grade Observability**
   - Complete tracing with correlation IDs
   - Performance metrics and SLA tracking
   - Error context propagation across layers

---

## Implementation Phases

### Phase 0: Foundation & Quick Wins (Week 1)
**Duration:** 5 days
**Goal:** Immediate quality improvements while preparing for major work

#### Tasks
1. **Enhance Prompt Engineering** (2 days)
   - Design comprehensive system prompt with five-phase doctrine
   - Add persona definition (expert SRE)
   - Include few-shot examples
   - Add tool usage instructions
   - Create prompt templates for each troubleshooting phase

2. **Improve Context Management** (1 day)
   - Increase context window from 5 to 15 messages
   - Add intelligent context ranking
   - Implement basic summarization for older messages

3. **Knowledge Base Enhancement** (2 days)
   - Add troubleshooting runbooks (K8s, Redis, PostgreSQL)
   - Improve document chunking strategy
   - Add metadata tags (technology, difficulty, category)
   - Create document ingestion pipeline

**Expected Impact:** +20-30% quality improvement
**Risk:** Low - No architectural changes

---

### Phase 1: Memory System Integration (Week 2)
**Duration:** 5 days
**Goal:** Restore and integrate hierarchical memory system

#### Tasks

**1.1 Restore Memory System Components** (1 day)
- Move `core/memory/` from backup to active codebase
- Verify all memory components are complete:
  - `memory_manager.py`
  - `hierarchical_memory.py`
  - Memory persistence interfaces

**1.2 Implement IMemoryService Interface** (1 day)
- Define complete IMemoryService interface in `models/interfaces.py`
- Create MemoryService in `services/domain/memory_service.py`
- Implement interface methods:
  - `store_conversation_turn()`
  - `retrieve_relevant_context()`
  - `consolidate_memory()`
  - `get_user_patterns()`

**1.3 Integrate with AgentStateManager** (2 days)
- Connect MemoryService to AgentStateManager
- Implement Redis persistence for memory
- Add memory consolidation scheduling
- Create memory retrieval strategies (recency, relevance, importance)

**1.4 Wire Memory into AgentService** (1 day)
- Inject MemoryService via container
- Add memory retrieval before query processing
- Store conversation results in memory
- Update container.py with memory service initialization

**1.5 Testing & Validation**
- Unit tests for MemoryService (50+ tests)
- Integration tests for memory persistence
- Multi-session memory retrieval tests
- Performance benchmarks for memory operations

**Expected Impact:** +20-25% quality improvement (context retention)
**Risk:** Medium - Complex state management

---

### Phase 2: Agentic Framework Orchestration (Week 3-4)
**Duration:** 10 days
**Goal:** Fully integrate 7-component agentic system into AgentService

#### Week 3: Core Integration

**2.1 Refactor AgentService Architecture** (2 days)
- Remove direct LLM calls from `process_query_for_case()`
- Design orchestration flow through all 7 components
- Create integration interfaces between components
- Add proper error handling and fallbacks

**2.2 Integrate Query Classification** (1 day)
- Wire QueryClassificationEngine into AgentService
- Add classification result to request context
- Route queries based on classification
- Log classification decisions for observability

**2.3 Connect Tool Broker** (2 days)
- Integrate ToolSkillBroker for dynamic tool selection
- Implement tool selection based on query classification
- Add tool execution coordination
- Create tool result aggregation logic

#### Week 4: Advanced Integration

**2.4 Implement Workflow Engine Integration** (3 days)
- Connect BusinessLogicWorkflowEngine
- Implement five-phase doctrine execution
- Add Plan→Execute→Observe→Re-plan cycles
- Create state transition logic for multi-turn reasoning

**2.5 Wire Response Synthesizer** (1 day)
- Integrate ResponseSynthesizer for multi-source assembly
- Add quality validation for responses
- Implement proper source attribution
- Format responses with markdown structure

**2.6 Add Guardrails Validation** (1 day)
- Connect GuardrailsPolicyLayer to validation flow
- Add PII protection checks before responses
- Implement output safety validation
- Create guardrail bypass for trusted operations

**2.7 Testing & Validation**
- Integration tests for full agentic flow (30+ tests)
- End-to-end workflow tests for each troubleshooting phase
- Performance benchmarks for orchestration overhead
- Regression tests to ensure no quality degradation

**Expected Impact:** +30-40% quality improvement (structured reasoning)
**Risk:** High - Major architectural changes

---

### Phase 3: Tool Ecosystem Expansion (Week 5)
**Duration:** 5 days
**Goal:** Expand agent capabilities with production-grade tools

#### Tasks

**3.1 Log Analysis Tool** (2 days)
- Implement LogAnalysisTool with BaseTool interface
- Add pattern detection (errors, warnings, OOM)
- Support multiple log formats (JSON, plain text, syslog)
- Add log correlation and timeline analysis
- Register in ToolRegistry

**3.2 Configuration Validation Tool** (1 day)
- Create ConfigValidationTool for YAML/JSON validation
- Add K8s manifest validation
- Check common misconfigurations
- Provide remediation suggestions

**3.3 Metrics Query Tool** (1 day)
- Implement MetricsQueryTool (if Prometheus available)
- Query system metrics (CPU, memory, network)
- Detect anomalies in metrics
- Correlate metrics with incidents

**3.4 Runbook Search Tool** (1 day)
- Create RunbookSearchTool for standard procedures
- Search operational runbooks
- Provide step-by-step guides
- Link to related documentation

**3.5 Testing & Documentation**
- Unit tests for each new tool (20+ tests per tool)
- Integration tests with ToolBroker
- Create tool usage documentation
- Add tool examples to prompts

**Expected Impact:** +15-20% quality improvement (actual capabilities)
**Risk:** Low - Additive changes

---

### Phase 4: User-Scoped Knowledge Base & Memory (Week 6)
**Duration:** 5 days
**Goal:** Implement user-specific knowledge base and personalized memory system

#### Tasks

**4.1 User-Scoped Knowledge Base Architecture** (2 days)
- Design user-specific ChromaDB collection strategy
- Implement UserKnowledgeService with user isolation
- Create user-specific document storage and retrieval
- Add user context to knowledge base queries
- Implement user knowledge base permissions and access control

**4.2 User-Specific Memory System** (2 days)
- Extend MemoryService to support user-scoped memory
- Implement user-specific pattern learning
- Create user profile and tech stack tracking
- Add user-specific memory consolidation
- Implement cross-user memory isolation

**4.3 User Profile & Personalization** (1 day)
- Create UserProfileService for tech stack tracking
- Implement user preference learning
- Add user-specific response customization
- Create user onboarding and profile setup
- Implement user-specific tool recommendations

**4.4 Testing & Integration**
- Unit tests for user-scoped services (40+ tests)
- Integration tests for user isolation
- Multi-user knowledge base tests
- User memory persistence tests
- Performance tests for user-scoped operations

**Expected Impact:** +25-30% personalization improvement
**Risk:** Medium - Complex user isolation and data management

---

### Phase 5: Context & Response Quality (Week 7)
**Duration:** 5 days
**Goal:** Advanced context management and response optimization

#### Tasks

**5.1 Intelligent Context Windowing** (2 days)
- Implement token budget management
- Add context ranking by relevance
- Create context summarization for older messages
- Preserve critical facts across summaries

**5.2 Enhanced Prompt Templates** (1 day)
- Create phase-specific prompts for five-phase doctrine
- Add domain-specific prompt variations
- Implement few-shot examples library
- Add output format templates

**5.3 Multi-Source Response Assembly** (1 day)
- Improve ResponseSynthesizer logic
- Add confidence scoring for sources
- Implement citation formatting
- Create response quality validation

**5.4 Response Personalization** (1 day)
- Use memory to personalize responses
- Adapt to user's technical level
- Reference past conversations
- Customize based on user's stack

**5.5 Testing & Optimization**
- Quality benchmarks (relevancy, accuracy, guidance)
- A/B testing different prompt variations
- Context window optimization tests
- Response time performance tests

**Expected Impact:** +10-15% quality improvement (polish)
**Risk:** Low - Incremental improvements

---

### Phase 6: ViewState & API Compliance (Week 8)
**Duration:** 5 days
**Goal:** Complete API implementation and frontend integration

#### Tasks

**6.1 Complete ViewState Implementation** (2 days)
- Implement full ViewState builder in AgentService
- Add all required fields:
  - `user` (from session context)
  - `active_case` (complete case data)
  - `cases` (list of user's cases)
  - `messages` (conversation history)
  - `uploaded_data` (file attachments)
  - `memory_context` (relevant memory)
  - `planning_state` (current troubleshooting phase)
  - `user_knowledge_base` (user-specific documents)
  - `user_profile` (tech stack, preferences)
- Create ViewState caching for performance

**6.2 API Schema Alignment** (1 day)
- Validate all responses against OpenAPI spec
- Add missing response fields
- Implement proper error responses
- Update API documentation

**6.3 Source Attribution Enhancement** (1 day)
- Implement detailed source tracking
- Add source metadata (confidence, timestamp)
- Create citation formatting
- Link sources to knowledge base entries

**6.4 Available Actions Implementation** (1 day)
- Populate AvailableAction based on agent state
- Suggest next steps based on troubleshooting phase
- Add quick actions for common operations
- Implement action validation

**6.5 Testing & Documentation**
- API contract tests (30+ tests)
- Frontend integration tests
- OpenAPI spec validation
- Update API documentation with examples

**Expected Impact:** Better frontend experience
**Risk:** Low - API contract implementation

---

### Phase 7: Observability & Production Readiness (Week 9)
**Duration:** 5 days
**Goal:** Complete observability and production preparation

#### Tasks

**7.1 Correlation ID Propagation** (2 days)
- Add correlation_id to all service method signatures
- Implement context propagation middleware
- Add tracing to all agentic framework components
- Create error context serialization

**7.2 Performance Monitoring** (1 day)
- Add metrics for each agentic component
- Track response quality metrics
- Monitor tool execution times
- Create SLA dashboards

**7.3 Error Recovery & Circuit Breakers** (1 day)
- Enhance ErrorFallbackManager integration
- Add circuit breakers for external services
- Implement retry strategies with backoff
- Create fallback response templates

**7.4 Production Configuration** (1 day)
- Create production environment configuration
- Document deployment procedures
- Add health check endpoints
- Create operational runbooks

**7.5 Load Testing & Optimization**
- Stress test full agentic pipeline
- Identify performance bottlenecks
- Optimize slow components
- Document performance characteristics

**Expected Impact:** Production-ready system
**Risk:** Low - Non-functional improvements

---

### Phase 8: Documentation & Knowledge Transfer (Week 10-11)
**Duration:** 10 days
**Goal:** Comprehensive documentation and team enablement

#### Tasks

**8.1 Architecture Documentation** (3 days)
- Update CLAUDE.md with final architecture
- Create detailed component interaction diagrams
- Document agentic framework flows
- Add memory system documentation
- Document user-scoped knowledge base architecture

**8.2 Developer Guides** (3 days)
- Create "Adding New Tools" guide
- Write "Extending Agentic Framework" guide
- Document prompt engineering best practices
- Add troubleshooting guides
- Create user-scoped feature development guide

**8.3 Operational Documentation** (2 days)
- Create deployment runbooks
- Document monitoring and alerting
- Add incident response procedures
- Create performance tuning guides

**8.4 API & Integration Documentation** (2 days)
- Update OpenAPI specification
- Create integration examples
- Document authentication flows
- Add SDK usage examples

**Expected Impact:** Team enablement
**Risk:** Low - Documentation

---

## Detailed Task Breakdown

### Phase 1 Deep Dive: Memory System Integration

#### 1.1 Restore Memory Components

**Files to Restore:**
```bash
# Move from backup
cp -r backup/faultmaven/core/memory/ faultmaven/core/memory/

# Expected structure:
faultmaven/core/memory/
├── __init__.py
├── memory_manager.py        # Main memory coordination
├── hierarchical_memory.py   # Short/long-term memory
└── consolidation.py         # Memory summarization
```

**Memory Architecture:**
```
Memory System (Three-Tier)
├── Working Memory (Current Session)
│   └── Last 10-15 conversation turns
├── Short-Term Memory (Recent Sessions)
│   └── Last 7 days, summarized
└── Long-Term Memory (Patterns & Facts)
    ├── User patterns (common issues)
    ├── System facts (tech stack)
    └── Learned knowledge
```

#### 1.2 IMemoryService Interface

**Location:** `faultmaven/models/interfaces.py`

```python
class IMemoryService(ABC):
    """Interface for memory management operations"""

    @abstractmethod
    async def store_conversation_turn(
        self,
        session_id: str,
        user_query: str,
        agent_response: str,
        metadata: Dict[str, Any]
    ) -> None:
        """Store a conversation turn in memory"""
        pass

    @abstractmethod
    async def retrieve_relevant_context(
        self,
        session_id: str,
        query: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Retrieve relevant context for current query"""
        pass

    @abstractmethod
    async def consolidate_memory(
        self,
        session_id: str
    ) -> Dict[str, Any]:
        """Consolidate and summarize session memory"""
        pass

    @abstractmethod
    async def get_user_patterns(
        self,
        user_id: str
    ) -> Dict[str, Any]:
        """Get learned patterns for user"""
        pass

    @abstractmethod
    async def extract_facts(
        self,
        session_id: str
    ) -> List[str]:
        """Extract important facts from conversation"""
        pass
```

#### 1.3 Memory Persistence Schema (Redis)

```python
# Redis Schema for Memory
memory:session:{session_id}:working -> List[ConversationTurn]
memory:session:{session_id}:facts -> List[ImportantFact]
memory:user:{user_id}:patterns -> Dict[pattern_type, List[Pattern]]
memory:user:{user_id}:stack -> Dict[technology, version]
memory:consolidation:{session_id} -> ConsolidatedSummary
```

**ConversationTurn Structure:**
```python
{
    "timestamp": "2025-09-30T10:30:00Z",
    "user_query": "Redis keeps crashing",
    "agent_response": "Let me check your logs...",
    "classification": {
        "intent": "troubleshooting",
        "domain": "database",
        "complexity": "moderate"
    },
    "tools_used": ["log_analysis", "knowledge_base"],
    "outcome": "identified_root_cause",
    "important": true  # Flag for consolidation
}
```

#### 1.4 Integration with AgentService

**Before (Current):**
```python
# agent_service.py - CURRENT
async def process_query_for_case(self, case_id: str, request: QueryRequest):
    # No memory retrieval
    sanitized_query = self._sanitizer.sanitize(request.query)
    llm_response = await self._call_llm_with_scenarios(...)
    return AgentResponse(content=llm_response)
```

**After (With Memory):**
```python
# agent_service.py - WITH MEMORY
async def process_query_for_case(self, case_id: str, request: QueryRequest):
    # 1. Retrieve relevant memory context
    memory_context = await self._memory_service.retrieve_relevant_context(
        session_id=request.session_id,
        query=request.query,
        limit=10
    )

    # 2. Get user patterns
    user_patterns = await self._memory_service.get_user_patterns(
        user_id=request.user_id
    )

    # 3. Process query with memory context
    sanitized_query = self._sanitizer.sanitize(request.query)

    # 4. Enrich prompt with memory
    enriched_prompt = self._build_memory_enriched_prompt(
        query=sanitized_query,
        memory_context=memory_context,
        user_patterns=user_patterns
    )

    # 5. Execute through agentic framework (Phase 2)
    response = await self._execute_agentic_workflow(enriched_prompt)

    # 6. Store conversation turn in memory
    await self._memory_service.store_conversation_turn(
        session_id=request.session_id,
        user_query=request.query,
        agent_response=response.content,
        metadata={
            "case_id": case_id,
            "classification": response.classification,
            "tools_used": response.tools_used
        }
    )

    return response
```

---

### Phase 4 Deep Dive: User-Scoped Knowledge Base & Memory

#### 4.1 User-Scoped Knowledge Base Architecture

**Current State:**
```python
# Current: Single shared knowledge base
collection = chroma_client.get_or_create_collection("faultmaven_kb")
# All users access same documents
```

**Target State:**
```python
# User-specific collections
user_collection = chroma_client.get_or_create_collection(
    name=f"faultmaven_kb_user_{user_id}",
    metadata={"user_id": user_id, "type": "user_specific"}
)

# Global shared collection
global_collection = chroma_client.get_or_create_collection(
    name="faultmaven_kb_global",
    metadata={"type": "shared"}
)
```

**UserKnowledgeService Implementation:**
```python
class UserKnowledgeService(IUserKnowledgeService):
    """Manages user-specific knowledge base operations"""
    
    async def search_user_knowledge(
        self,
        user_id: str,
        query: str,
        include_global: bool = True,
        limit: int = 10
    ) -> List[KnowledgeResult]:
        """Search user-specific and optionally global knowledge"""
        
        results = []
        
        # Search user-specific knowledge
        user_results = await self._search_user_collection(user_id, query, limit)
        results.extend(user_results)
        
        # Search global knowledge if requested
        if include_global:
            global_results = await self._search_global_collection(query, limit)
            results.extend(global_results)
        
        # Rank and deduplicate results
        return self._rank_and_deduplicate(results, limit)
    
    async def store_user_document(
        self,
        user_id: str,
        document: UserDocument,
        embeddings: List[float]
    ) -> str:
        """Store document in user-specific collection"""
        
        collection = await self._get_user_collection(user_id)
        
        document_id = f"user_{user_id}_{document.id}"
        
        collection.add(
            documents=[document.content],
            embeddings=[embeddings],
            metadatas=[{
                "user_id": user_id,
                "title": document.title,
                "type": document.type,
                "category": document.category,
                "tags": document.tags,
                "source": "user_upload"
            }],
            ids=[document_id]
        )
        
        return document_id
```

#### 4.2 User-Specific Memory System

**Memory Persistence Schema (Redis):**
```python
# User-specific memory keys
memory:user:{user_id}:patterns -> Dict[pattern_type, List[Pattern]]
memory:user:{user_id}:stack -> Dict[technology, version]
memory:user:{user_id}:preferences -> Dict[preference, value]
memory:user:{user_id}:learning -> Dict[concept, mastery_level]

# Session memory (user-scoped)
memory:session:{session_id}:user_context -> UserContext
memory:session:{session_id}:personalized_insights -> List[Insight]
```

**UserMemoryService Implementation:**
```python
class UserMemoryService(IUserMemoryService):
    """Manages user-specific memory and learning patterns"""
    
    async def learn_user_pattern(
        self,
        user_id: str,
        pattern_type: str,
        pattern_data: Dict[str, Any]
    ) -> None:
        """Learn and store user-specific patterns"""
        
        # Get existing patterns
        existing_patterns = await self._get_user_patterns(user_id, pattern_type)
        
        # Update pattern with new data
        updated_pattern = self._update_pattern(existing_patterns, pattern_data)
        
        # Store updated pattern
        await self._redis_client.hset(
            f"memory:user:{user_id}:patterns",
            pattern_type,
            json.dumps(updated_pattern)
        )
    
    async def get_personalized_context(
        self,
        user_id: str,
        query: str
    ) -> PersonalizedContext:
        """Get personalized context for user"""
        
        # Get user patterns
        patterns = await self._get_user_patterns(user_id)
        
        # Get user tech stack
        tech_stack = await self._get_user_tech_stack(user_id)
        
        # Get user preferences
        preferences = await self._get_user_preferences(user_id)
        
        # Build personalized context
        return PersonalizedContext(
            patterns=patterns,
            tech_stack=tech_stack,
            preferences=preferences,
            personalized_prompt=self._build_personalized_prompt(
                query, patterns, tech_stack, preferences
            )
        )
```

#### 4.3 User Profile & Personalization

**UserProfileService Implementation:**
```python
class UserProfileService(IUserProfileService):
    """Manages user profiles and personalization"""
    
    async def update_user_tech_stack(
        self,
        user_id: str,
        technology: str,
        version: str,
        confidence: float = 1.0
    ) -> None:
        """Update user's technology stack"""
        
        current_stack = await self._get_user_tech_stack(user_id)
        
        # Update or add technology
        current_stack[technology] = {
            "version": version,
            "confidence": confidence,
            "last_updated": datetime.utcnow().isoformat()
        }
        
        # Store updated stack
        await self._redis_client.hset(
            f"memory:user:{user_id}:stack",
            technology,
            json.dumps(current_stack[technology])
        )
    
    async def get_user_expertise_level(
        self,
        user_id: str,
        domain: str
    ) -> str:
        """Determine user's expertise level in domain"""
        
        # Analyze user's interaction history
        interactions = await self._get_user_interactions(user_id, domain)
        
        # Calculate expertise based on:
        # - Query complexity handled
        # - Successful problem resolution
        # - Tool usage sophistication
        # - Response quality feedback
        
        expertise_score = self._calculate_expertise_score(interactions)
        
        if expertise_score >= 0.8:
            return "expert"
        elif expertise_score >= 0.6:
            return "advanced"
        elif expertise_score >= 0.4:
            return "intermediate"
        else:
            return "beginner"
```

#### 4.4 Integration with AgentService

**Enhanced AgentService with User Context:**
```python
async def process_query_for_case(self, case_id: str, request: QueryRequest):
    # 1. Get user context
    user_context = await self._user_profile_service.get_user_context(
        user_id=request.user_id
    )
    
    # 2. Retrieve user-specific memory
    user_memory = await self._user_memory_service.get_personalized_context(
        user_id=request.user_id,
        query=request.query
    )
    
    # 3. Search user-specific knowledge
    user_knowledge = await self._user_knowledge_service.search_user_knowledge(
        user_id=request.user_id,
        query=request.query,
        include_global=True
    )
    
    # 4. Build personalized prompt
    personalized_prompt = self._build_personalized_prompt(
        query=request.query,
        user_context=user_context,
        user_memory=user_memory,
        user_knowledge=user_knowledge
    )
    
    # 5. Execute through agentic framework
    response = await self._execute_agentic_workflow(personalized_prompt)
    
    # 6. Learn from interaction
    await self._user_memory_service.learn_from_interaction(
        user_id=request.user_id,
        query=request.query,
        response=response,
        user_feedback=None  # Could be collected later
    )
    
    return response
```

---

### Phase 2 Deep Dive: Agentic Framework Orchestration

#### 2.1 Current vs Target Flow

**Current Flow (Basic):**
```
User Query → Sanitize → Add Context → LLM Call → Return Response
```

**Target Flow (Agentic):**
```
User Query
    ↓
1. QueryClassificationEngine
    ├─ Intent: troubleshooting | status | explanation
    ├─ Complexity: simple | moderate | complex | expert
    ├─ Domain: database | networking | infrastructure
    └─ Urgency: critical | high | medium | low
    ↓
2. AgentStateManager (Memory Retrieval)
    ├─ Working memory (recent turns)
    ├─ Relevant facts from history
    ├─ User patterns
    └─ Current troubleshooting state
    ↓
3. ToolSkillBroker (Tool Selection)
    ├─ Based on classification
    ├─ Based on current phase
    └─ Dynamic capability discovery
    ↓
4. BusinessLogicWorkflowEngine (Reasoning)
    ├─ Phase 1: Define Blast Radius
    ├─ Phase 2: Establish Timeline
    ├─ Phase 3: Formulate Hypothesis
    ├─ Phase 4: Validate Hypothesis
    ├─ Phase 5: Propose Solution
    └─ Plan → Execute → Observe → Re-plan
    ↓
5. GuardrailsPolicyLayer (Validation)
    ├─ PII protection check
    ├─ Safety validation
    └─ Output sanitization
    ↓
6. ResponseSynthesizer (Assembly)
    ├─ Multi-source integration
    ├─ Format standardization
    ├─ Source attribution
    └─ Quality validation
    ↓
7. ErrorFallbackManager (Safety Net)
    ├─ Error recovery
    ├─ Circuit breaker
    └─ Graceful degradation
    ↓
Response to User
```

#### 2.2 AgentService Refactor

**New AgentService Structure:**

```python
# faultmaven/services/agentic/orchestration/agent_service.py

class AgentService(BaseService):
    """Orchestrates the 7-component agentic framework"""

    def __init__(
        self,
        # Core dependencies
        llm_provider: ILLMProvider,
        sanitizer: ISanitizer,
        tracer: ITracer,

        # 7 Agentic Components
        query_classification_engine: IQueryClassificationEngine,
        agent_state_manager: IAgentStateManager,
        tool_skill_broker: IToolSkillBroker,
        business_logic_workflow_engine: IBusinessLogicWorkflowEngine,
        guardrails_policy_layer: IGuardrailsPolicyLayer,
        response_synthesizer: IResponseSynthesizer,
        error_fallback_manager: IErrorFallbackManager,

        # Supporting services
        memory_service: IMemoryService,
        session_service: ISessionService,
        settings: Any
    ):
        super().__init__()
        # Store all dependencies

    async def process_query_for_case(
        self,
        case_id: str,
        request: QueryRequest
    ) -> AgentResponse:
        """Process query through full agentic framework"""

        try:
            # Step 1: Sanitize input
            sanitized_query = self._sanitizer.sanitize(request.query)

            # Step 2: Classify query
            classification = await self.query_classification_engine.classify_query(
                query=sanitized_query,
                context={"case_id": case_id, "session_id": request.session_id}
            )

            # Step 3: Load agent state (memory)
            agent_state = await self.agent_state_manager.load_state(
                session_id=request.session_id,
                case_id=case_id
            )

            # Step 4: Select tools based on classification
            selected_tools = await self.tool_skill_broker.select_tools(
                classification=classification,
                agent_state=agent_state
            )

            # Step 5: Execute workflow with five-phase doctrine
            workflow_result = await self.business_logic_workflow_engine.execute(
                query=sanitized_query,
                classification=classification,
                agent_state=agent_state,
                tools=selected_tools,
                llm_provider=self._llm_provider
            )

            # Step 6: Validate with guardrails
            validated_result = await self.guardrails_policy_layer.validate(
                result=workflow_result,
                classification=classification
            )

            # Step 7: Synthesize final response
            response = await self.response_synthesizer.synthesize(
                workflow_result=validated_result,
                classification=classification,
                agent_state=agent_state
            )

            # Step 8: Update agent state
            await self.agent_state_manager.save_state(
                session_id=request.session_id,
                case_id=case_id,
                agent_state=workflow_result.updated_state
            )

            # Step 9: Record in memory
            await self._memory_service.store_conversation_turn(
                session_id=request.session_id,
                user_query=request.query,
                agent_response=response.content,
                metadata=workflow_result.metadata
            )

            return response

        except Exception as e:
            # Step 10: Error recovery
            return await self.error_fallback_manager.handle_error(
                error=e,
                context={
                    "query": request.query,
                    "case_id": case_id,
                    "session_id": request.session_id
                }
            )
```

#### 2.3 Five-Phase Workflow Execution

**BusinessLogicWorkflowEngine Implementation:**

```python
# faultmaven/services/agentic/engines/workflow_engine.py

class BusinessLogicWorkflowEngine(IBusinessLogicWorkflowEngine):
    """Implements five-phase SRE troubleshooting doctrine"""

    async def execute(
        self,
        query: str,
        classification: QueryClassification,
        agent_state: AgentState,
        tools: List[BaseTool],
        llm_provider: ILLMProvider
    ) -> WorkflowResult:
        """Execute workflow based on classification"""

        # Determine current phase from agent state
        current_phase = agent_state.current_phase or Phase.DEFINE_BLAST_RADIUS

        # Get phase-specific guidance
        phase_config = TroubleshootingDoctrine.get_phase_config(current_phase)

        # Build phase-specific prompt
        prompt = self._build_phase_prompt(
            query=query,
            phase=current_phase,
            phase_config=phase_config,
            agent_state=agent_state,
            available_tools=[tool.name for tool in tools]
        )

        # Execute LLM reasoning with tools
        reasoning_result = await self._execute_llm_with_tools(
            prompt=prompt,
            tools=tools,
            llm_provider=llm_provider
        )

        # Determine if phase is complete
        phase_complete = self._assess_phase_completion(
            reasoning_result=reasoning_result,
            phase=current_phase,
            phase_config=phase_config
        )

        # Advance to next phase if complete
        next_phase = self._determine_next_phase(
            current_phase=current_phase,
            phase_complete=phase_complete,
            reasoning_result=reasoning_result
        )

        # Update agent state
        updated_state = self._update_agent_state(
            agent_state=agent_state,
            current_phase=next_phase,
            reasoning_result=reasoning_result
        )

        return WorkflowResult(
            reasoning=reasoning_result.content,
            current_phase=next_phase,
            phase_complete=phase_complete,
            tools_used=reasoning_result.tools_used,
            updated_state=updated_state,
            metadata=reasoning_result.metadata
        )

    def _build_phase_prompt(
        self,
        query: str,
        phase: Phase,
        phase_config: Dict[str, Any],
        agent_state: AgentState,
        available_tools: List[str]
    ) -> str:
        """Build phase-specific prompt with guidance"""

        prompt_parts = [
            f"# CURRENT PHASE: {phase.value.upper()}",
            f"\n## Objective\n{phase_config['objective']}",
            f"\n## Key Questions to Answer",
            *[f"- {q}" for q in phase_config['key_questions']],
            f"\n## Available Tools\n{', '.join(available_tools)}",
            f"\n## Success Criteria",
            *[f"- {c}" for c in phase_config['success_criteria']],
            f"\n## Agent State",
            f"Previous findings: {agent_state.findings}",
            f"Current hypotheses: {agent_state.hypotheses}",
            f"\n## User Query\n{query}",
            f"\n## Instructions",
            f"Based on the {phase.value} phase, analyze the query and provide insights.",
            f"Use available tools as needed. Be specific and actionable."
        ]

        return "\n".join(prompt_parts)
```

---

### Phase 3 Deep Dive: Tool Ecosystem Expansion

#### 3.1 Tool Architecture

**BaseTool Interface (Already Defined):**
```python
class BaseTool(ABC):
    """Base interface for all agent tools"""

    @abstractmethod
    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """Execute the tool with given parameters"""
        pass

    @abstractmethod
    def get_schema(self) -> Dict[str, Any]:
        """Get tool schema for LLM function calling"""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Tool description"""
        pass
```

#### 3.2 LogAnalysisTool Implementation

**File:** `faultmaven/tools/log_analysis.py`

```python
class LogAnalysisTool(BaseTool):
    """Analyze logs for errors, patterns, and anomalies"""

    name = "log_analysis"
    description = """Analyze log files to identify errors, warnings, patterns, and anomalies.
    Supports JSON logs, plain text logs, and structured logs.
    Use this tool when troubleshooting issues that may be evident in logs."""

    def __init__(self, llm_provider: ILLMProvider):
        self.llm_provider = llm_provider
        self._init_patterns()

    def _init_patterns(self):
        """Initialize common log patterns"""
        self.error_patterns = [
            r'ERROR|Error|error:',
            r'FATAL|Fatal|fatal:',
            r'Exception|exception',
            r'failed|FAILED',
            r'OOMKilled',
            r'connection refused',
            r'timeout|timed out',
        ]

        self.warning_patterns = [
            r'WARN|Warning|warning:',
            r'deprecated',
            r'retry|retrying',
        ]

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """Analyze logs"""
        log_content = params.get("log_content", "")
        log_type = params.get("log_type", "auto")  # auto, json, plain

        if not log_content:
            return ToolResult(
                success=False,
                data={"error": "No log content provided"},
                metadata={}
            )

        # Parse logs
        parsed_logs = self._parse_logs(log_content, log_type)

        # Detect patterns
        errors = self._detect_errors(parsed_logs)
        warnings = self._detect_warnings(parsed_logs)
        patterns = self._detect_patterns(parsed_logs)

        # Build timeline
        timeline = self._build_timeline(parsed_logs, errors)

        # Analyze with LLM for deeper insights
        llm_analysis = await self._llm_analyze(
            errors=errors,
            warnings=warnings,
            patterns=patterns,
            timeline=timeline
        )

        return ToolResult(
            success=True,
            data={
                "summary": llm_analysis["summary"],
                "errors_found": len(errors),
                "warnings_found": len(warnings),
                "error_details": errors[:10],  # Top 10
                "patterns": patterns,
                "timeline": timeline,
                "recommendations": llm_analysis["recommendations"]
            },
            metadata={
                "total_lines": len(parsed_logs),
                "log_type": log_type
            }
        )

    def _parse_logs(self, content: str, log_type: str) -> List[Dict]:
        """Parse log content into structured format"""
        # Implementation for JSON, plain text, syslog formats
        pass

    def _detect_errors(self, logs: List[Dict]) -> List[Dict]:
        """Detect error entries in logs"""
        # Implementation using regex patterns
        pass

    def _detect_patterns(self, logs: List[Dict]) -> List[Dict]:
        """Detect repeating patterns"""
        # Implementation for pattern detection
        pass

    async def _llm_analyze(self, **kwargs) -> Dict[str, Any]:
        """Use LLM for deeper log analysis"""
        prompt = f"""Analyze these log patterns:

Errors: {kwargs['errors'][:5]}
Warnings: {kwargs['warnings'][:5]}
Patterns: {kwargs['patterns']}
Timeline: {kwargs['timeline']}

Provide:
1. Summary of what went wrong
2. Root cause hypothesis
3. Specific recommendations"""

        response = await self.llm_provider.generate(
            prompt=prompt,
            temperature=0.3,
            max_tokens=500
        )

        return {
            "summary": response,
            "recommendations": self._extract_recommendations(response)
        }

    def get_schema(self) -> Dict[str, Any]:
        """Tool schema for function calling"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "log_content": {
                        "type": "string",
                        "description": "The log content to analyze"
                    },
                    "log_type": {
                        "type": "string",
                        "enum": ["auto", "json", "plain", "syslog"],
                        "description": "Log format type"
                    }
                },
                "required": ["log_content"]
            }
        }
```

#### 3.3 Additional Tools (Brief Descriptions)

**ConfigValidationTool:**
```python
class ConfigValidationTool(BaseTool):
    """Validate K8s manifests, Docker configs, env files"""

    async def execute(self, params):
        config = params["config"]
        config_type = params["type"]  # k8s, docker, env

        # Validate syntax
        # Check common misconfigurations
        # Provide remediation suggestions

        return ToolResult(...)
```

**MetricsQueryTool:**
```python
class MetricsQueryTool(BaseTool):
    """Query Prometheus/Grafana metrics"""

    async def execute(self, params):
        query = params["query"]
        time_range = params.get("time_range", "5m")

        # Query metrics backend
        # Detect anomalies
        # Correlate with incidents

        return ToolResult(...)
```

**RunbookSearchTool:**
```python
class RunbookSearchTool(BaseTool):
    """Search operational runbooks and procedures"""

    async def execute(self, params):
        query = params["query"]
        category = params.get("category")  # deployment, incident, maintenance

        # Search runbook database
        # Return step-by-step procedures
        # Link related documentation

        return ToolResult(...)
```

---

## Success Criteria

### Phase 0: Foundation & Quick Wins
- ✅ System prompt includes five-phase doctrine
- ✅ Context window increased to 15 messages
- ✅ 50+ troubleshooting documents in knowledge base
- ✅ Measured +20% quality improvement on test set

### Phase 1: Memory System
- ✅ Memory components restored from backup
- ✅ IMemoryService fully implemented (100+ unit tests)
- ✅ Redis persistence working with <100ms latency
- ✅ Memory retrieval in AgentService
- ✅ Multi-session memory tests passing
- ✅ Measured +20% context retention in conversations

### Phase 2: Agentic Framework
- ✅ All 7 components integrated in AgentService
- ✅ Query classification routing working
- ✅ Five-phase workflow executing correctly
- ✅ 30+ integration tests passing
- ✅ Tool selection dynamic based on classification
- ✅ Response synthesis from multiple sources
- ✅ Measured +30% improvement in structured reasoning

### Phase 3: Tool Ecosystem
- ✅ 3+ new tools implemented (Log, Config, Metrics/Runbook)
- ✅ Each tool has 20+ unit tests
- ✅ Tools registered in ToolRegistry
- ✅ ToolBroker selecting tools correctly
- ✅ Tool results integrated into responses
- ✅ Measured +15% improvement in actionable guidance

### Phase 4: User-Scoped Knowledge Base & Memory
- ✅ UserKnowledgeService implemented with user isolation
- ✅ User-specific ChromaDB collections working
- ✅ UserMemoryService with personalized patterns
- ✅ UserProfileService for tech stack tracking
- ✅ Cross-user data isolation verified
- ✅ Measured +25% personalization improvement

### Phase 5: Context & Response Quality
- ✅ Intelligent context ranking implemented
- ✅ Token budget management working
- ✅ Phase-specific prompts created
- ✅ Response personalization based on memory
- ✅ Quality benchmarks show +10% improvement

### Phase 6: ViewState & API
- ✅ ViewState includes all required fields
- ✅ API responses match OpenAPI spec (100%)
- ✅ Source attribution complete
- ✅ Available actions populated
- ✅ Frontend integration tests passing

### Phase 7: Observability
- ✅ Correlation IDs propagate through all layers
- ✅ Metrics dashboards for agentic components
- ✅ Circuit breakers prevent cascading failures
- ✅ Load tests show <2s p95 response time
- ✅ Health checks cover all components

### Phase 8: Documentation
- ✅ Architecture diagrams updated
- ✅ Developer guides complete
- ✅ Operational runbooks created
- ✅ API documentation with examples

### Overall System Quality Metrics

**Response Quality (Target: +70% improvement)**
- Relevancy: User confirms answer addresses their question (>85%)
- Accuracy: Information is factually correct (>90%)
- Guidance: Response includes actionable steps (>80%)
- Completeness: User doesn't need to ask follow-ups (>70%)

**System Performance**
- Response time p95: <2 seconds
- Response time p99: <5 seconds
- Memory retrieval: <100ms
- Tool execution: <1s average
- Availability: >99.5%

**Code Quality**
- Test coverage: >75%
- All integration tests passing
- Zero critical security issues
- Documentation coverage: 100% of public APIs

---

## Risk Mitigation

### High-Risk Items

**1. Agentic Framework Integration (Phase 2)**

**Risk:** Major refactor could break existing functionality
**Mitigation:**
- Create feature branch for integration work
- Maintain backward compatibility during transition
- Comprehensive regression test suite
- Gradual rollout (10% → 50% → 100% traffic)
- Monitoring dashboards to detect quality degradation
- Rollback plan prepared

**2. Memory System Complexity**

**Risk:** Memory persistence issues or performance degradation
**Mitigation:**
- Redis backup strategy with replication
- Memory retrieval caching (TTL: 5 minutes)
- Circuit breakers for memory service
- Fallback to stateless operation if memory unavailable
- Performance benchmarks before production

**3. LLM Token Budget Overruns**

**Risk:** Context window + memory exceeds token limits
**Mitigation:**
- Token counting before LLM calls
- Intelligent summarization of older context
- Configurable token budgets per component
- Graceful degradation (reduce context if needed)

### Medium-Risk Items

**4. Tool Execution Timeouts**

**Risk:** Tools take too long, blocking user response
**Mitigation:**
- Tool execution timeouts (5s default)
- Async tool execution where possible
- Fallback responses if tools timeout
- Tool health monitoring

**5. Response Quality Regression**

**Risk:** Changes accidentally reduce quality
**Mitigation:**
- Quality benchmark test suite
- A/B testing for major changes
- User feedback collection
- Rollback triggers if quality drops >10%

### Low-Risk Items

**6. Documentation Drift**

**Risk:** Documentation becomes outdated
**Mitigation:**
- Documentation updates in same PR as code changes
- Automated OpenAPI spec generation
- Regular documentation review cycles

---

## Testing Strategy

### Test Pyramid

```
                    ▲
                   ╱ ╲
                  ╱   ╲
                 ╱ E2E ╲          ~50 tests (5%)
                ╱───────╲
               ╱         ╲
              ╱Integration╲       ~200 tests (15%)
             ╱─────────────╲
            ╱               ╲
           ╱  Unit Tests     ╲    ~1200 tests (80%)
          ╱───────────────────╲
         ╱                     ╲
        ▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
```

### Testing Approach by Phase

**Phase 1: Memory System**
```bash
# Unit tests (80 tests)
pytest tests/services/domain/test_memory_service.py -v
pytest tests/core/memory/test_hierarchical_memory.py -v

# Integration tests (20 tests)
pytest tests/integration/test_memory_persistence.py -v
pytest tests/integration/test_memory_retrieval.py -v

# Performance tests (5 tests)
pytest tests/performance/test_memory_latency.py -v
```

**Phase 2: Agentic Framework**
```bash
# Component unit tests (150 tests)
pytest tests/services/agentic/ -v

# Integration tests (30 tests)
pytest tests/integration/test_agentic_workflow.py -v
pytest tests/integration/test_five_phase_doctrine.py -v

# End-to-end tests (10 tests)
pytest tests/e2e/test_full_troubleshooting_flow.py -v
```

**Phase 3: Tool Ecosystem**
```bash
# Tool unit tests (60 tests, 20 per tool)
pytest tests/tools/test_log_analysis.py -v
pytest tests/tools/test_config_validation.py -v
pytest tests/tools/test_metrics_query.py -v

# Tool integration tests (15 tests)
pytest tests/integration/test_tool_broker_selection.py -v
```

### Quality Benchmark Suite

**Create:** `tests/quality/benchmark_suite.py`

```python
# Quality benchmark tests
test_cases = [
    {
        "query": "My Kubernetes pod keeps restarting",
        "expected_phase": Phase.DEFINE_BLAST_RADIUS,
        "expected_tools": ["log_analysis", "knowledge_base"],
        "quality_criteria": {
            "mentions_pod_status": True,
            "asks_for_logs": True,
            "relevancy_score": 0.8
        }
    },
    # ... 50+ benchmark test cases
]
```

**Run benchmarks:**
```bash
pytest tests/quality/benchmark_suite.py --benchmark
```

---

## Deployment Plan

### Environment Strategy

**1. Development (Local)**
- All services in Docker Compose
- Local LLM for development
- Redis/ChromaDB local instances
- Hot reload enabled

**2. Staging (K8s)**
- Full K8s cluster (faultmaven-k8s-infra)
- Production-like configuration
- Real external services (Presidio, Opik)
- Synthetic load testing

**3. Production (K8s)**
- HA setup with 3+ replicas
- Production LLM providers
- Full observability stack
- Blue-green deployment

### Phased Rollout Strategy

**Week 1-6: Development**
- All changes in `feature/agentic-integration` branch
- Local testing and validation
- Team review and feedback

**Week 7: Staging Deployment**
- Deploy to staging K8s cluster
- Run full integration test suite
- Performance testing and optimization
- Security scanning

**Week 8: Production Canary (10% traffic)**
- Deploy to production with 10% traffic
- Monitor quality metrics closely
- Compare against baseline (old system)
- Collect user feedback

**Week 9: Production Ramp (50% traffic)**
- Increase to 50% if metrics positive
- Continue monitoring
- Address any issues found

**Week 10: Full Production (100% traffic)**
- Complete rollout if successful
- Decommission old system
- Finalize documentation

### Rollback Plan

**Trigger Conditions:**
- Response quality drops >10%
- Error rate increases >5%
- P95 latency increases >50%
- User complaints increase significantly

**Rollback Procedure:**
```bash
# Immediate traffic shift back to old system
kubectl set image deployment/faultmaven-api api=faultmaven:v1.0.0

# Verify rollback
kubectl rollout status deployment/faultmaven-api

# Investigate issues
kubectl logs -l app=faultmaven-api --tail=100
```

---

## Monitoring & Observability

### Key Metrics

**Response Quality Metrics**
```
# Prometheus metrics
faultmaven_response_relevancy_score histogram
faultmaven_response_accuracy_score histogram
faultmaven_response_completeness_score histogram
faultmaven_user_satisfaction_rating histogram
```

**Agentic Framework Metrics**
```
faultmaven_classification_duration_seconds histogram
faultmaven_memory_retrieval_duration_seconds histogram
faultmaven_tool_execution_duration_seconds histogram by tool
faultmaven_workflow_phase_transitions counter by phase
faultmaven_llm_tokens_used counter by provider
```

**System Health Metrics**
```
faultmaven_requests_total counter by endpoint
faultmaven_request_duration_seconds histogram
faultmaven_errors_total counter by type
faultmaven_circuit_breaker_state gauge by component
```

### Dashboards

**1. Response Quality Dashboard**
- Average relevancy/accuracy/guidance scores
- Quality score distribution
- Low-quality response alerts
- User feedback trends

**2. Agentic Framework Dashboard**
- Component execution times
- Phase transition flow diagram
- Tool usage heatmap
- Memory hit/miss rates

**3. System Performance Dashboard**
- Request rate and latency
- Error rates by component
- Circuit breaker states
- Resource utilization

### Alerts

**Critical Alerts (PagerDuty)**
- System unavailable (>5% errors)
- Response time >5s p95
- Memory service down
- LLM provider failures

**Warning Alerts (Slack)**
- Quality score drops >10%
- Slow tool executions
- High memory retrieval latency
- Circuit breaker opened

---

## Timeline Summary

| Phase | Duration | Week | Key Deliverables |
|-------|----------|------|------------------|
| Phase 0 | 5 days | Week 1 | Enhanced prompts, better context, enriched KB |
| Phase 1 | 5 days | Week 2 | Memory system integrated |
| Phase 2 | 10 days | Week 3-4 | Agentic framework orchestrated |
| Phase 3 | 5 days | Week 5 | 3+ new tools added |
| Phase 4 | 5 days | Week 6 | User-scoped KB & memory system |
| Phase 5 | 5 days | Week 7 | Context & response optimization |
| Phase 6 | 5 days | Week 8 | ViewState & API complete |
| Phase 7 | 5 days | Week 9 | Observability & production prep |
| Phase 8 | 10 days | Week 10-11 | Documentation & rollout |

**Total Duration:** 9-11 weeks
**Estimated Effort:** 360-440 hours (2 full-time developers)

---

## Appendix A: File Changes Checklist

### Files to Create

**Memory System**
- `faultmaven/core/memory/memory_manager.py`
- `faultmaven/core/memory/hierarchical_memory.py`
- `faultmaven/core/memory/consolidation.py`
- `faultmaven/services/domain/memory_service.py`
- `faultmaven/services/domain/user_memory_service.py`
- `faultmaven/services/domain/user_profile_service.py`
- `tests/services/domain/test_memory_service.py`
- `tests/services/domain/test_user_memory_service.py`
- `tests/core/memory/test_hierarchical_memory.py`

**User-Scoped Knowledge Base**
- `faultmaven/services/domain/user_knowledge_service.py`
- `faultmaven/core/knowledge/user_knowledge_manager.py`
- `faultmaven/infrastructure/persistence/user_knowledge_store.py`
- `tests/services/domain/test_user_knowledge_service.py`
- `tests/core/knowledge/test_user_knowledge_manager.py`

**Tools**
- `faultmaven/tools/log_analysis.py`
- `faultmaven/tools/config_validation.py`
- `faultmaven/tools/metrics_query.py`
- `faultmaven/tools/runbook_search.py`
- `tests/tools/test_log_analysis.py` (+ others)

**Prompts**
- `faultmaven/prompts/system_prompts.py`
- `faultmaven/prompts/phase_prompts.py`
- `faultmaven/prompts/few_shot_examples.py`

**Documentation**
- `docs/architecture/AGENTIC_FRAMEWORK.md`
- `docs/architecture/MEMORY_SYSTEM.md`
- `docs/guides/ADDING_TOOLS.md`
- `docs/guides/PROMPT_ENGINEERING.md`

### Files to Modify

**Core Integration**
- `faultmaven/services/agentic/orchestration/agent_service.py` (major refactor)
- `faultmaven/container.py` (add memory service, user services)
- `faultmaven/models/interfaces.py` (add IMemoryService, IUserKnowledgeService, IUserMemoryService)

**Configuration**
- `.env.example` (add memory/tool configs)
- `faultmaven/config/settings.py` (add settings)

**Documentation**
- `CLAUDE.md` (update architecture)
- `README.md` (update features)
- `docs/api/openapi.yaml` (update ViewState)

---

## Appendix B: Development Environment Setup

### Prerequisites
```bash
# Python 3.11+
python --version

# Redis (for memory)
redis-cli ping

# Docker (for services)
docker --version

# K8s cluster (optional, for staging)
kubectl cluster-info
```

### Local Development Setup
```bash
# 1. Clone and setup
cd /home/swhouse/projects/FaultMaven
python -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt
pip install -r requirements-test.txt

# 3. Start services
docker-compose up -d redis chromadb

# 4. Configure environment
cp .env.example .env
# Edit .env with your API keys

# 5. Run tests
pytest tests/ -v

# 6. Start development server
./run_faultmaven.sh
```

### Development Workflow
```bash
# Create feature branch
git checkout -b feature/phase-1-memory-system

# Make changes
# ... code ...

# Run tests
pytest tests/services/domain/test_memory_service.py -v

# Check code quality
black faultmaven tests
flake8 faultmaven tests
mypy faultmaven

# Commit
git add .
git commit -m "Phase 1: Implement memory service"

# Push and create PR
git push origin feature/phase-1-memory-system
```

---

## Appendix C: References

### Architecture Documents
- `docs/architecture/SYSTEM_ARCHITECTURE.md` - Current system architecture
- `docs/architecture/COMPONENT_INTERACTIONS.md` - Component interaction patterns
- `docs/specifications/CASE_SESSION_CONCEPTS.md` - Session management concepts

### Code References
- `faultmaven/services/agentic/` - Agentic framework components
- `faultmaven/core/agent/doctrine.py` - Five-phase SRE doctrine
- `faultmaven/container.py` - Dependency injection container

### External Resources
- LangGraph Documentation: https://langchain-ai.github.io/langgraph/
- Redis Documentation: https://redis.io/docs/
- ChromaDB Documentation: https://docs.trychroma.com/

---

## Sign-Off

This implementation plan provides a comprehensive roadmap for transforming FaultMaven into a well-structured, robust, powerful, scalable, and expandable AI troubleshooting system.

**Expected Outcomes:**
- 📈 **70% improvement in response quality** (relevancy, accuracy, guidance)
- 🏗️ **Production-ready architecture** with full agentic framework integration
- 🧠 **Intelligent memory system** with context-aware learning
- 🛠️ **Extensible tool ecosystem** for rapid capability expansion
- 📊 **Complete observability** for production operations

**Next Steps:**
1. Review and approve this plan
2. Set up project tracking (Jira/Linear)
3. Begin Phase 0 (immediate quality improvements)
4. Schedule regular progress reviews (weekly)

---

**Document Status:** Ready for Review
**Prepared by:** Claude Code
**Date:** 2025-09-30