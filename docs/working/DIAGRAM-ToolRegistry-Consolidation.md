# ToolRegistry Consolidation - Architecture Diagrams

## Current State (Before Consolidation)

```mermaid
graph TB
    subgraph "Test Code"
        T[test_tool_registry.py]
    end

    subgraph "registry.py Module"
        TR1[ToolRegistry<br/>Class-based<br/>Incomplete API]
        GR1[tool_registry<br/>Global Singleton]
        TR1 --> GR1
    end

    subgraph "base.py Module"
        TR2[AgentToolRegistry<br/>Instance-based<br/>Complete API]
        GR2[tool_registry<br/>Global Singleton]
        TR2 --> GR2
    end

    subgraph "Interfaces"
        BT[BaseTool<br/>interface]
        AT[AgentTool<br/>extends BaseTool]
    end

    T -->|imports from| TR1
    T -->|expects API from| TR2
    TR1 -.->|uses| BT
    TR2 -->|uses| AT
    AT -->|extends| BT

    style T fill:#f99
    style TR1 fill:#faa
    style TR2 fill:#afa
    style GR1 fill:#faa
    style GR2 fill:#afa

    classDef problem fill:#f99
    classDef legacy fill:#faa
    classDef modern fill:#afa
```

**Problem**: Tests import from `registry.py` but expect `AgentToolRegistry` API from `base.py`. Two competing implementations cause confusion and test failures.

---

## Proposed State (After Consolidation)

```mermaid
graph TB
    subgraph "Test Code"
        T[test_tool_registry.py]
    end

    subgraph "registry.py Module"
        RE[Re-export:<br/>ToolRegistry = AgentToolRegistry<br/>tool_registry imported]
        DEC[register_tool<br/>Decorator<br/>Deprecated]
    end

    subgraph "base.py Module"
        TR[AgentToolRegistry<br/>Canonical Implementation]
        GR[tool_registry<br/>Global Singleton]
        TR --> GR
    end

    subgraph "Interfaces"
        BT[BaseTool<br/>interface]
        AT[AgentTool<br/>extends BaseTool]
    end

    subgraph "Application Code"
        APP[Tool Registration<br/>at Startup]
        EX[Agent Execution]
    end

    T -->|imports from| RE
    RE -->|re-exports| TR
    RE --> DEC
    DEC -.->|legacy support| TR
    TR -->|uses| AT
    AT -->|extends| BT
    APP -->|register instances| GR
    EX -->|execute_tool| GR

    style T fill:#afa
    style RE fill:#9cf
    style TR fill:#afa
    style GR fill:#afa
    style APP fill:#afa
    style EX fill:#afa

    classDef success fill:#afa
    classDef export fill:#9cf
```

**Solution**: Single canonical implementation in `base.py`, re-exported from `registry.py` for backward compatibility. All code uses the same registry instance.

---

## Component Interaction - Before

```mermaid
sequenceDiagram
    participant Test as Test Suite
    participant RegistryPy as registry.py<br/>(ToolRegistry)
    participant BasePy as base.py<br/>(AgentToolRegistry)

    Test->>RegistryPy: register(tool_name, ToolClass)
    RegistryPy-->>Test: ❌ TypeError: Expected 2 args, got 1

    Test->>RegistryPy: get(tool_name)
    RegistryPy-->>Test: ❌ AttributeError: No 'get' method

    Test->>RegistryPy: execute_tool(name, params, context)
    RegistryPy-->>Test: ❌ AttributeError: No 'execute_tool' method

    Note over Test,RegistryPy: Tests fail because registry.py<br/>doesn't have expected API

    Note over BasePy: AgentToolRegistry has the correct API<br/>but tests don't import it
```

---

## Component Interaction - After

```mermaid
sequenceDiagram
    participant Test as Test Suite
    participant RegistryPy as registry.py<br/>(Re-export)
    participant BasePy as base.py<br/>(AgentToolRegistry)
    participant Tool as AgentTool Instance

    Test->>RegistryPy: from registry import ToolRegistry
    RegistryPy->>BasePy: Import AgentToolRegistry
    RegistryPy-->>Test: ✅ ToolRegistry (alias)

    Test->>RegistryPy: tool_registry.register(tool_instance)
    RegistryPy->>BasePy: Forward to AgentToolRegistry
    BasePy->>BasePy: Store in _tools dict
    BasePy-->>Test: ✅ Registered

    Test->>RegistryPy: tool_registry.get("tool_name")
    RegistryPy->>BasePy: Forward to AgentToolRegistry
    BasePy->>BasePy: Lookup in _tools dict
    BasePy-->>Test: ✅ Tool instance

    Test->>RegistryPy: tool_registry.execute_tool(name, params, ctx)
    RegistryPy->>BasePy: Forward to AgentToolRegistry
    BasePy->>BasePy: Validate params
    BasePy->>Tool: execute_with_context(params, ctx)
    Tool-->>BasePy: ToolResult
    BasePy-->>Test: ✅ ToolResult
```

---

## Data Model - Registry Internal Storage

### Before: Two Separate Registries

```mermaid
graph LR
    subgraph "registry.py Storage"
        R1[_tools: Dict]
        R1 --> C1["'kb': KnowledgeBaseTool (Class)"]
        R1 --> C2["'evidence': EvidenceTool (Class)"]
    end

    subgraph "base.py Storage"
        R2[_tools: Dict]
        R2 --> I1["'kb': kb_instance (Instance)"]
        R2 --> I2["'evidence': evidence_instance (Instance)"]
    end

    style R1 fill:#faa
    style R2 fill:#afa
```

### After: Single Unified Registry

```mermaid
graph LR
    subgraph "AgentToolRegistry Storage (base.py)"
        R[_tools: Dict]
        R --> I1["'knowledge_base':<br/>KnowledgeBaseTool(kb_client)"]
        R --> I2["'evidence_reader':<br/>EvidenceTool(evidence_svc)"]
        R --> I3["'file_search':<br/>FileSearchTool(search_svc)"]
    end

    subgraph "Re-export (registry.py)"
        EX["tool_registry = base.tool_registry"]
    end

    EX -.->|references| R

    style R fill:#afa
    style EX fill:#9cf
```

---

## Registration Patterns Comparison

### Pattern 1: Class-based (Deprecated)

```mermaid
graph TD
    A[Define Tool Class] --> B[@register_tool decorator]
    B --> C[Class registered with registry]
    C --> D[First get call]
    D --> E[Instantiate tool]
    E --> F[Cache instance]
    F --> G[Return instance]

    style A fill:#faa
    style B fill:#faa
    style C fill:#faa
```

**Problems**:
- Hidden initialization
- No dependency injection
- Circular import risks
- Testing difficulties

### Pattern 2: Instance-based (Recommended)

```mermaid
graph TD
    A[Define Tool Class] --> B[Create dependencies]
    B --> C[Instantiate tool with deps]
    C --> D[Register instance]
    D --> E[Tool immediately available]
    E --> F[get returns cached instance]

    style A fill:#afa
    style B fill:#afa
    style C fill:#afa
    style D fill:#afa
    style E fill:#afa
    style F fill:#afa
```

**Benefits**:
- Explicit initialization
- Clear dependency injection
- Easy testing with mocks
- No circular imports

---

## Tool Execution Flow

```mermaid
sequenceDiagram
    participant Agent as Agent Workflow
    participant Registry as tool_registry
    participant Tool as AgentTool Instance
    participant Context as ToolContext
    participant Service as External Service

    Agent->>Registry: execute_tool("kb_search", params, context)
    activate Registry

    Registry->>Registry: get("kb_search")
    Registry->>Registry: Validate params against schema

    alt Invalid params
        Registry-->>Agent: ToolResult(success=False, error="...")
    else Valid params
        Registry->>Tool: execute_with_context(params, context)
        activate Tool

        Tool->>Context: Extract session_id, case_id
        Tool->>Service: Query knowledge base
        Service-->>Tool: Search results

        Tool->>Tool: Format results
        Tool-->>Registry: ToolResult(success=True, data={...})
        deactivate Tool

        Registry-->>Agent: ToolResult
    end
    deactivate Registry

    Agent->>Agent: Process tool result
```

---

## Testing Strategy Diagram

```mermaid
graph TB
    subgraph "Unit Tests"
        U1[test_registry_register_tool]
        U2[test_registry_get_tool]
        U3[test_registry_execute_tool]
        U4[test_registry_validation]
        U5[test_registry_error_handling]
    end

    subgraph "Integration Tests"
        I1[test_agent_uses_registered_tools]
        I2[test_tool_execution_with_context]
        I3[test_multiple_tools_workflow]
    end

    subgraph "Registry Implementation"
        R[AgentToolRegistry]
    end

    U1 --> R
    U2 --> R
    U3 --> R
    U4 --> R
    U5 --> R

    I1 --> R
    I2 --> R
    I3 --> R

    R --> C[Coverage: 80%+]

    style U1 fill:#afa
    style U2 fill:#afa
    style U3 fill:#afa
    style U4 fill:#afa
    style U5 fill:#afa
    style I1 fill:#aaf
    style I2 fill:#aaf
    style I3 fill:#aaf
    style C fill:#afa
```

---

## Deployment Architecture

### Before: Confusion at Startup

```mermaid
graph TB
    subgraph "Application Startup"
        START[main.py] --> INIT[Initialize modules]
        INIT --> REG1[registry.py: tool_registry]
        INIT --> REG2[base.py: tool_registry]
    end

    subgraph "Runtime Behavior"
        REG1 -.->|different instance| STORE1[Class storage]
        REG2 -.->|different instance| STORE2[Instance storage]
    end

    subgraph "Problems"
        P1[Two singletons]
        P2[State inconsistency]
        P3[Tool missing from one registry]
    end

    STORE1 --> P1
    STORE2 --> P1
    P1 --> P2
    P2 --> P3

    style START fill:#faa
    style REG1 fill:#faa
    style REG2 fill:#faa
    style P1 fill:#f99
    style P2 fill:#f99
    style P3 fill:#f99
```

### After: Single Source of Truth

```mermaid
graph TB
    subgraph "Application Startup"
        START[main.py] --> INIT[Initialize modules]
        INIT --> DEPS[Create tool dependencies]
        DEPS --> TOOLS[Instantiate tools]
        TOOLS --> REG[Register with tool_registry]
    end

    subgraph "Registry (base.py)"
        REG --> STORE[Single _tools dict]
    end

    subgraph "Re-export (registry.py)"
        EXPORT[tool_registry = base.tool_registry]
    end

    STORE -.->|same instance| EXPORT

    subgraph "Runtime Behavior"
        APP1[Agent Module] --> STORE
        APP2[Test Suite] --> EXPORT
        EXPORT -.->|references| STORE
    end

    style START fill:#afa
    style DEPS fill:#afa
    style TOOLS fill:#afa
    style REG fill:#afa
    style STORE fill:#afa
    style EXPORT fill:#9cf
    style APP1 fill:#afa
    style APP2 fill:#afa
```

---

## Migration Path

```mermaid
graph LR
    subgraph "Phase 1: Consolidation"
        P1A[Update registry.py<br/>Re-export AgentToolRegistry]
        P1B[Run tests<br/>Verify all pass]
        P1C[Deploy to dev]
    end

    subgraph "Phase 2: Deprecation"
        P2A[Add deprecation warnings<br/>to @register_tool]
        P2B[Update internal code<br/>to instance-based]
        P2C[Document migration]
    end

    subgraph "Phase 3: Cleanup"
        P3A[Remove @register_tool<br/>decorator]
        P3B[Remove class-based<br/>support]
        P3C[Update docs]
    end

    P1A --> P1B --> P1C
    P1C --> P2A
    P2A --> P2B --> P2C
    P2C --> P3A
    P3A --> P3B --> P3C

    style P1A fill:#9cf
    style P1B fill:#9cf
    style P1C fill:#9cf
    style P2A fill:#ff9
    style P2B fill:#ff9
    style P2C fill:#ff9
    style P3A fill:#faa
    style P3B fill:#faa
    style P3C fill:#faa
```

**Timeline**:
- Phase 1: 1 day (immediate fix)
- Phase 2: 2-4 weeks (gradual migration)
- Phase 3: 1-2 sprints (final cleanup)

---

## Success Metrics

```mermaid
graph TB
    subgraph "Before Consolidation"
        B1[15 failing tests]
        B2[2 registry implementations]
        B3[Confusion about which to use]
        B4[Inconsistent registration patterns]
    end

    subgraph "After Consolidation"
        A1[0 failing tests ✅]
        A2[1 canonical registry ✅]
        A3[Clear API documentation ✅]
        A4[Single registration pattern ✅]
        A5[Improved test coverage ✅]
    end

    B1 -.->|fix| A1
    B2 -.->|consolidate| A2
    B3 -.->|document| A3
    B4 -.->|standardize| A4
    A1 --> A5

    style B1 fill:#f99
    style B2 fill:#f99
    style B3 fill:#f99
    style B4 fill:#f99
    style A1 fill:#afa
    style A2 fill:#afa
    style A3 fill:#afa
    style A4 fill:#afa
    style A5 fill:#afa
```

---

## API Before/After Comparison

### Before: Inconsistent APIs

```mermaid
graph TB
    subgraph "registry.py API"
        R1[register name, class]
        R2[get_tool name → class]
        R3[list_tools → names]
        R4[create_all_tools → instances]
    end

    subgraph "base.py API"
        B1[register instance]
        B2[get name → instance]
        B3[list_tools → names]
        B4[get_all_tools → instances]
        B5[execute_tool name, params, ctx]
        B6[get_all_schemas → schemas]
        B7[get_all_domain_tools → tools]
        B8[clear]
    end

    style R1 fill:#faa
    style R2 fill:#faa
    style R3 fill:#ff9
    style R4 fill:#faa
    style B1 fill:#afa
    style B2 fill:#afa
    style B3 fill:#ff9
    style B4 fill:#afa
    style B5 fill:#afa
    style B6 fill:#afa
    style B7 fill:#afa
    style B8 fill:#afa
```

### After: Single Consistent API

```mermaid
graph TB
    subgraph "Canonical API (from registry.py or base.py)"
        A1[register instance]
        A2[get name → instance]
        A3[list_tools → names]
        A4[get_all_tools → instances]
        A5[get_all_schemas → schemas]
        A6[get_all_domain_tools → tools]
        A7[execute_tool name, params, ctx]
        A8[clear for testing]
    end

    style A1 fill:#afa
    style A2 fill:#afa
    style A3 fill:#afa
    style A4 fill:#afa
    style A5 fill:#afa
    style A6 fill:#afa
    style A7 fill:#afa
    style A8 fill:#afa
```

---

## End State Architecture

```mermaid
graph TB
    subgraph "Public API (registry.py)"
        API[ToolRegistry<br/>tool_registry<br/>register_tool]
    end

    subgraph "Implementation (base.py)"
        IMPL[AgentToolRegistry<br/>Complete implementation<br/>All methods]
    end

    subgraph "Interfaces (models/interfaces.py)"
        BT[BaseTool]
        AT[AgentTool]
        BT --> AT
    end

    subgraph "Domain Models (domain/events.py)"
        DT[Tool<br/>Domain model for LLM API]
    end

    subgraph "Application Code"
        STARTUP[Startup:<br/>Register tool instances]
        AGENT[Agent Workflow:<br/>Execute tools]
        TESTS[Test Suite:<br/>Verify behavior]
    end

    API -.->|re-exports| IMPL
    IMPL -->|uses| AT
    AT -->|extends| BT
    AT -->|converts to| DT

    STARTUP -->|register| API
    AGENT -->|execute| API
    TESTS -->|verify| API

    style API fill:#9cf
    style IMPL fill:#afa
    style AT fill:#afa
    style BT fill:#afa
    style DT fill:#afa
    style STARTUP fill:#afa
    style AGENT fill:#afa
    style TESTS fill:#afa
```

**Key Points**:
1. Single source of truth: `AgentToolRegistry` in `base.py`
2. Public interface: Re-exported from `registry.py` for discoverability
3. Clean separation: Interfaces → Implementation → Domain models
4. All application code uses the same registry instance
5. Tests verify canonical behavior

---

## Conclusion

The consolidation eliminates architectural inconsistency by:
- **Reducing complexity**: 2 registries → 1 registry
- **Fixing tests**: 15 failures → 0 failures
- **Improving clarity**: Canonical API with clear documentation
- **Enabling evolution**: Single implementation to maintain and enhance

Implementation is straightforward: update `registry.py` to re-export `AgentToolRegistry` from `base.py`.
