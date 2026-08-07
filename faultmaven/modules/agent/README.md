# Agent Module

**Type**: Domain Service — business logic only, owns no database tables.

---

## Overview

The Agent module supplies the investigation turn workflow and the tool set the
milestone engine calls during an investigation. It is a leaf: nothing imports
it, and it owns no persistence of its own — anything it stores goes through the
Case module's contracts.

It exposes **no HTTP routes**. Investigation turns arrive through the Case
module's turn endpoint and are driven by `core/investigation/milestone_engine.py`.
The agent-execution endpoints (`POST /cases/{id}/sessions/{sid}/execute`, with
its SSE streaming variant) were removed along with the `AgentOrchestrationService`
they drove; the milestone engine had already taken over that work, and nothing
called them.

---

## Module Structure

```
faultmaven/modules/agent/
├── api/
│   └── __init__.py             # No routes — see Overview
├── domain/
│   ├── models/
│   │   └── agentic.py          # Agentic framework models, QueryIntent, SuggestedAction
│   └── services/
│       ├── investigation_service.py  # Turn lifecycle around the MilestoneEngine
│       └── query_classifier.py       # Deterministic processing-mode classifier
├── jobs/
│   └── storage_cleanup.py      # TTL-based orphan-file sweep
├── tools/
│   ├── base.py                 # AgentTool base class, ToolContext, Tool
│   ├── list_evidence_tool.py
│   ├── list_evidence_by_time_tool.py
│   ├── list_top_entities_tool.py
│   ├── find_entity_tool.py
│   ├── read_file_tool.py
│   ├── reclassify_evidence_tool.py
│   ├── vectorize_file_tool.py
│   ├── case_evidence_qa.py     # Case-scoped evidence Q&A
│   ├── kb_qa.py                # Unified KB Q&A (global + personal + team)
│   ├── kb_tool_adapter.py      # AgentTool wrapper for kb_qa
│   ├── document_qa_tool.py     # KB-neutral base class (strategy pattern)
│   ├── search_file_tool.py     # Tier 2 mechanical search (keyword/regex/extractor)
│   ├── deep_analysis_tool.py   # Tier 3 deep LLM analysis
│   ├── web_search.py           # Web search tool
│   ├── kb_config.py            # Abstract KBConfig strategy interface
│   └── kb_configs/             # Knowledge base configurations
│       ├── case_evidence_config.py
│       └── unified_kb_config.py
└── exceptions.py
```

There is no `infrastructure/`: as a Domain Service the module owns no tables, so
it has no repositories. Execution audit data (`agent_executions`,
`agent_tool_calls`) is **Case-owned** and reached through
`modules/case/contracts.py`.

---

## Services

### InvestigationService

Wraps the `MilestoneEngine` with the turn lifecycle around it: access control,
case retrieval and persistence, turn creation and processing, progress
reporting, and session integration.

### QueryClassifier

Classifies a user message into a processing mode — `TRIAGE`,
`DIRECTED_ANALYSIS`, or `KNOWLEDGE_QUERY` — from entity-detection heuristics.
Deterministic and LLM-free, so it is cheap enough to run on every turn.

---

## Tools

Tools are constructed explicitly via DI during container initialization; there
is no self-registering tool registry.

**Evidence tools** (read through the Evidence module):

- `ListEvidenceTool` — list available evidence artifacts
- `ListEvidenceByTimeTool` — evidence within a time window
- `ListTopEntitiesTool` — most-referenced entities across the case
- `FindEntityTool` — locate a named entity in case evidence
- `ReadFileTool` — read evidence file contents
- `ReclassifyEvidenceTool` — correct an evidence classification
- `VectorizeFileTool` — vectorize a file into the case vector store
- `CaseEvidenceQATool` — Q&A over case evidence
- `SearchFileTool` — Tier 2 mechanical search (keyword/regex/extractor), with
  two-pass keyword matching and zero-result vocabulary recovery
- `DeepAnalysisTool` — Tier 3 deep LLM analysis with pluggable backends
  (external, local, basic)

**Knowledge tools**:

- `AnswerFromKB` (via `KBToolAdapter`, tool name `kb_qa`) — unified KB Q&A,
  searching every accessible scope (global + personal + team) in one query
- `DocumentQATool` — KB-neutral base class (strategy pattern with `KBConfig`)

**Web tools**:

- `WebSearchTool` — web search capability

---

## Dependencies

### What the Agent module imports

| Module/Layer | Usage |
|--------------|-------|
| `core/investigation/` | Milestone engine, hypothesis manager, intent resolver |
| `infrastructure/llm/` | LLM provider abstractions and routing |
| `modules/case/contracts` | `Case`, `ICaseRepository`, and the Case-owned execution models |
| `modules/evidence/` | Evidence retrieval for tools |
| `modules/knowledge/` | Knowledge base and vector search for tools |

### What imports the Agent module

**Nothing.** Agent is a leaf module: it sits at the top of the dependency graph
and consumes services from the modules below it. If the Case module ever needs
agent status, that goes through domain events rather than a direct import.

---

## Architectural Decisions

### `core/investigation/` stays shared infrastructure

The milestone engine and hypothesis manager are not agent-module internals —
they are the investigation core, and they stay in `core/investigation/`. Agent
services import from there.

```python
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
```

### LLM infrastructure stays shared

LLM providers and routing live in `infrastructure/llm/`, not here. Report needs
them for summaries and Knowledge needs them for embeddings, so they belong below
every module that calls them rather than inside one.

### All tools live in this module

Tools stay in `modules/agent/tools/` rather than being distributed to the
modules whose data they read. One location means one place to look and one
construction path; the dependency on Evidence and Knowledge is explicit in the
imports.

---

## Usage

```python
# Execution audit models — Case-owned, imported from Case contracts
from faultmaven.modules.case.contracts import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)

# Tools
from faultmaven.modules.agent.tools.base import AgentTool, Tool, ToolContext
```

The module does not eagerly import its submodules — import them directly, as
above, rather than expecting attributes on the package.

---

## Testing

**Unit tests** — tool behaviour per tool class, model validation, and the
query classifier's mode boundaries.

**Integration tests** — tool invocation against real Evidence and Knowledge
services, and turn processing through `InvestigationService`.
