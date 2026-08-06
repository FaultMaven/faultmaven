# Agent Module

**Type**: Domain services + tool system
**HTTP surface**: none

---

## Overview

The Agent module owns the tools the investigation engine calls, the service that
runs an investigation turn, and the agent-facing domain models.

It has no API layer. Investigation traffic enters through the Case module's
`/turns` endpoints and is driven by `core/investigation/milestone_engine`, which
is where tool execution, LLM calls, context budgeting and the deep-analysis
safety nets live. The module's own router and the `AgentOrchestrationService`
behind it were a second, unreachable copy of that path and were deleted in #982.

---

## Module Structure

```text
faultmaven/modules/agent/
├── exceptions.py
├── domain/
│   ├── models/
│   │   └── agentic.py                 # QueryIntent, SuggestedAction
│   ├── events/
│   │   └── execution_events.py        # ExecutionEvent and streaming event types
│   └── services/
│       ├── investigation_service.py   # Investigation turn lifecycle
│       └── query_classifier.py        # Intent classification for a user turn
├── jobs/
│   └── storage_cleanup.py             # Orphaned evidence-file reaper
└── tools/
    ├── base.py                        # AgentTool, ToolContext, AgentToolRegistry
    ├── list_evidence_tool.py          # ListEvidenceTool, SearchKnowledgeTool
    ├── list_evidence_by_time_tool.py
    ├── list_top_entities_tool.py
    ├── find_entity_tool.py
    ├── read_file_tool.py
    ├── search_file_tool.py            # Tier 2 mechanical search (keyword/regex)
    ├── deep_analysis_tool.py          # Tier 3 deep LLM analysis
    ├── vectorize_file_tool.py
    ├── reclassify_evidence_tool.py
    ├── case_evidence_qa.py            # AnswerFromCaseEvidence
    ├── kb_qa.py                       # AnswerFromKB
    ├── kb_tool_adapter.py             # KBToolAdapter, CaseEvidenceQAAdapter
    ├── document_qa_tool.py            # KB-neutral base (strategy pattern)
    ├── kb_config.py                   # KBConfig strategy interface
    └── kb_configs/
        ├── case_evidence_config.py
        └── unified_kb_config.py
```

---

## Domain

### InvestigationService

- **Responsibility**: investigation turn lifecycle and milestone coordination
- **Dependencies**: MilestoneEngine, CaseRepository, preprocessing service, file storage service
- **Key operations**:
  - Process investigation turns (user input → milestone advancement)
  - Coordinate evidence intake through preprocessing
  - Resolve user intent against pending agent suggestions
  - Manage milestone progress and status transitions

Its intent dispatch is validated for completeness at construction time — a
`QueryIntent` with no handler fails fast rather than falling through silently.

### QueryClassifier

Classifies a user turn into a `QueryIntent`. Imported lazily at both call sites
(`InvestigationService` and `milestone_engine`) to keep the import graph acyclic.

### Models and events

`domain/models/agentic.py` holds `QueryIntent` and `SuggestedAction`.
`domain/events/execution_events.py` holds the execution and streaming event
types. Agent-execution audit records (`AgentExecution`, `AgentToolCall`,
`AgentType`, `ExecutionStatus`) are **Case-owned** and imported from
`modules.case.contracts`; that persistence subsystem is dormant — see the note on
`ICaseRepository`.

---

## Tools

Registered tool names, as the LLM sees them:

**Evidence**
- `list_evidence` — list available evidence artifacts
- `list_evidence_by_time` — evidence within a time window
- `list_top_entities` — most frequent entities across evidence
- `find_entity` — locate a specific entity
- `read_file` — read evidence file contents
- `search_file` — Tier 2 mechanical search (keyword/regex/extractor), two-pass keyword matching with zero-result vocabulary recovery
- `deep_analysis` — Tier 3 deep LLM analysis with pluggable backends (external, local, basic)
- `vectorize_file` — vectorize an evidence file on demand
- `reclassify_evidence` — delegates to `InvestigationService.reclassify_evidence`

**Knowledge**
- `kb_qa` — unified KB Q&A via `KBToolAdapter`, searching all accessible scopes (global + personal + team) in one query
- `case_evidence_search` — case-scoped evidence Q&A via `CaseEvidenceQAAdapter`
- `search_knowledge` — knowledge search

**Web**
- `web_search`

**Tool system**
- `AgentTool` — base class
- `ToolContext` — per-invocation context (case, user, team ids)
- `AgentToolRegistry` / `tool_registry` — registration and discovery
- `DocumentQATool` + `KBConfig` — strategy pattern so one Q&A implementation serves several knowledge bases

Tools are constructed explicitly via DI in `container/providers/tools.py` and
handed to the engine as `investigation_tools`.

---

## Dependencies

### What the Agent module imports from

| Module/Layer | Usage |
|--------------|-------|
| **core/investigation/** | Milestone engine, hypothesis manager |
| **infrastructure/llm/** | LLM provider abstractions and routing |
| **modules/case/** | Case context and contracts (`Case`, `ICaseRepository`, agent-execution models) |
| **modules/evidence/** | Evidence retrieval and file storage for tools |
| **modules/knowledge/** | Knowledge base access and vector search for tools |

### What imports the Agent module

`core/investigation/milestone_engine` imports `query_classifier`, and
`container/providers/` constructs the tools and `InvestigationService`. No other
vertical module depends on Agent.

---

## Architectural Decisions

### 1. `core/investigation/` stays shared infrastructure

The milestone engine and hypothesis manager are not Agent-module-private; they
are the investigation path itself. Agent services import from
`faultmaven.core.investigation.*` rather than owning it.

### 2. All tools live in `modules/agent/tools/`

Single registry, single owner, simple discovery. Tools import from the Evidence
and Knowledge services, so the dependency is explicit rather than hidden.

### 3. LLM infrastructure stays in `infrastructure/llm/`

Report and Knowledge need LLM access too, and provider routing, fallback chains
and response caching are already well-abstracted there. The module has no LLM
client of its own — `llm_client.py` was part of the shadow stack and went with it
in #982.

### 4. No API layer

Everything the module does is reached through the Case module's `/turns`
endpoints. A second HTTP entry point was what made the shadow stack possible.

---

## Usage Examples

```python
# Agent-execution audit models (Case-owned)
from faultmaven.modules.case.contracts import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)

# Agent domain models
from faultmaven.modules.agent.domain.models.agentic import QueryIntent, SuggestedAction

# Events
from faultmaven.modules.agent.domain.events.execution_events import ExecutionEvent

# Tools — constructed via DI during container initialization
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext
```

The packages do not eagerly import their contents, to keep the import graph
acyclic. What each advertises in `__all__` is checked by
`tests/unit/modules/agent/test_package_exports.py`.

---

## Testing

- **Unit**: per-tool tests under `tests/unit/modules/agent/tools/`, investigation
  service and intent-dispatch tests, package-export resolution
- **Integration**: tool invocation against real Evidence/Knowledge services
- Investigation turn behaviour is covered where the engine lives, under
  `tests/unit/core/investigation/`

---

## Future Improvements

**Distributed tools.** Evidence tools could move to `modules/evidence/tools/` and
knowledge tools to `modules/knowledge/tools/`, with plugin-based discovery — better
cohesion, at the cost of a single registry.

**Execution audit.** The Case-owned `agent_executions` schema currently has no
writer. If milestone turns should be audited, that is the schema to adopt; if
not, it should be removed.
