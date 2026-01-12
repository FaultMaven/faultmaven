# Agent Module Extraction Plan - Key Revisions

**Date**: 2026-01-06
**Status**: Feedback Incorporated, Ready for Review

---

## Summary

The Agent module extraction plan has been **significantly simplified** based on critical feedback, reducing timeline from **5 weeks to 2-3 weeks** and eliminating over-engineering risks.

---

## Critical Changes Made

### 1. ❌ NO Investigation Core Library

**Original Plan:**
- Create `faultmaven/investigation/` as new top-level package
- Move 7 files (~2,000 LOC) from `core/investigation/`
- Requires import updates across codebase

**Revised Plan:**
- ✅ **Keep `core/investigation/` exactly as-is (NO MOVE)**
- Agent services import from existing location
- Reduces complexity and risk
- No benefit to moving since no other modules need it

**Impact:** Saves 3-5 days of work, reduces blast radius

---

### 2. ❌ NO Tool Distribution Across Modules

**Original Plan:**
- Move evidence tools → `modules/evidence/tools/`
- Move knowledge tools → `modules/knowledge/tools/`
- Implement plugin-based tool discovery
- Modify Evidence and Knowledge modules during Agent extraction

**Revised Plan:**
- ✅ **ALL tools in `modules/agent/tools/` initially**
- Single registry, simple discovery
- No changes to Evidence/Knowledge modules
- Tool distribution can be future improvement

**Impact:** Saves 2-3 days of work, reduces coordination complexity

---

### 3. ✅ FIRM Service Separation Decision

**Original Plan:**
- "Start with Option B and refactor to Option A if complexity becomes unmanageable"
- Non-decision creating uncertainty

**Revised Plan:**
- ✅ **Commit to Option B (Clear Separation of Concerns)**
- Three services with distinct responsibilities:

| Service | Level | Responsibility |
|---------|-------|----------------|
| `AgentOrchestrationService` | Low | LLM calls, streaming, tool execution |
| `InvestigationOrchestrator` | Mid | Workflow state, phase transitions |
| `InvestigationService` | High | Milestone tracking, OODA coordination |

- No merging during extraction
- If merging needed, do as separate refactor AFTER extraction

**Impact:** Clear implementation path, no mid-extraction refactoring

---

### 4. 🔧 Safer Import Updates

**Original Plan:**
- Use `sed -i` for mass find-and-replace across codebase
- Risky, hard to review, no rollback

**Revised Plan:**
- ✅ **Use `grep` to find files needing updates**
- Manual or IDE refactoring for actual changes
- Verify each change before committing
- Safer and more controlled

**Impact:** Reduces risk of breaking imports unexpectedly

---

### 5. 📊 Reduced File Count Estimate

**Original Plan:**
- 35-40 production files
- 49 test files

**Revised Plan:**
- ✅ **~25-30 production files** (removed 7 core/investigation files)
- ✅ **~40-45 test files** (verification needed)
- More accurate scope

**Impact:** More realistic timeline estimate

---

## Simplified Timeline

### Original: 5 Weeks

1. Week 1: Preparation & Investigation Core library
2. Week 2: Infrastructure & Events
3. Week 3: Domain Models & Services
4. Week 4: Tool System & API
5. Week 5: Testing & Finalization

### Revised: 2-3 Weeks

1. **3-4 days**: Module structure, models, events
2. **3-4 days**: Services extraction
3. **3-4 days**: API routes, tools, repositories
4. **3-4 days**: Testing, integration, cleanup

---

## What Stays Unchanged (NO MOVE)

1. **`core/investigation/`** - Shared investigation infrastructure

   ```python
   # Agent services import from existing location
   from faultmaven.core.investigation.ooda_engine import OODAEngine
   from faultmaven.core.investigation.milestone_engine import MilestoneEngine
   ```

2. **`infrastructure/llm/`** - LLM provider implementations

   ```python
   # Agent services import from shared infrastructure
   from faultmaven.infrastructure.llm.providers import OpenAIProvider
   ```

3. **`integrations/llm_client.py`** - LLM client wrapper

   ```python
   # Agent services import from shared infrastructure
   from faultmaven.integrations.llm_client import LLMClient
   ```

---

## What Moves to Agent Module

### Production Files (~25-30)

**Services (3 files):**
1. `services/agent_orchestration_service.py`
2. `services/domain/investigation_orchestrator.py`
3. `services/domain/investigation_service.py`

**Models (3 files):**
4. `models/agent_execution.py`
5. `models/agentic.py`
6. `models/investigation.py` (if not in core/)

**API (1 file):**
7. `api/routes/agent.py`

**Infrastructure (1 file):**
8. `infrastructure/persistence/agent_execution_repository.py`

**Tools (11 files):**
9. `tools/base.py`
10. `tools/registry.py`
11. `tools/list_evidence_tool.py`
12. `tools/read_file_tool.py`
13. `tools/case_evidence_qa.py`
14. `tools/knowledge_base.py`
15. `tools/user_kb_qa.py`
16. `tools/global_kb_qa.py`
17. `tools/document_qa_tool.py`
18. `tools/web_search.py`
19-21. `tools/kb_configs/` (3 files)

**Domain Events:**
22. Create `modules/agent/domain/events/execution_events.py` (extract from `domain/events.py`)

---

## Benefits of Revisions

### Reduced Risk

- ✅ No new top-level packages during extraction
- ✅ No modifications to Evidence/Knowledge modules
- ✅ No complex plugin discovery system
- ✅ Smaller blast radius

### Faster Execution

- ✅ 2-3 weeks instead of 5 weeks
- ✅ Less coordination required
- ✅ Simpler implementation

### Clearer Decisions

- ✅ Firm service separation (no "maybe merge later")
- ✅ Clear "what stays, what moves"
- ✅ Executable plan with concrete steps

### Future Flexibility

- ✅ Tool distribution can be done later if needed
- ✅ Investigation Core can be extracted later if other modules need it
- ✅ Service merging can be separate refactor if beneficial

---

## Comparison Matrix

| Aspect | Original Plan | Revised Plan |
|--------|--------------|--------------|
| **Timeline** | 5 weeks | 2-3 weeks |
| **Investigation Core** | Create new library | Keep as-is |
| **Tools** | Distribute to modules | All in Agent module |
| **Service Decision** | Deferred ("maybe merge") | Firm (clear separation) |
| **Import Updates** | sed mass replacement | grep + manual |
| **File Count** | 35-40 + 49 tests | 25-30 + 40-45 tests |
| **Blast Radius** | High (3 modules) | Low (Agent only) |
| **Complexity** | High | Medium |
| **Risk** | High (many moving parts) | Medium (focused) |

---

## Next Steps

1. **Team Review** of revised plan
2. **Verify File Counts** using grep/find
3. **Approve** simplified extraction strategy
4. **Kick-off Phase 1** when ready

---

## References

- [AGENT-MODULE-EXTRACTION-PLAN.md](./AGENT-MODULE-EXTRACTION-PLAN.md) - Full revised plan
- [MODULE-EXTRACTION-STATUS.md](./MODULE-EXTRACTION-STATUS.md) - Overall platform status

---

**Status**: Pragmatic, executable plan ready for team review and approval
