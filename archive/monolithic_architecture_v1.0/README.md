# Monolithic Architecture v1.0 - ARCHIVED

**Archive Date:** 2025-10-06
**Superseded By:** Sub-Agent Architecture (Anthropic Context Engineering Pattern)

## What Was Archived

This directory contains the original monolithic doctor/patient architecture implementation that used a single powerful LLM call with full context.

### Archived Components:

1. **turn_processor.py** - Main monolithic turn processing logic
   - Single LLM call with ~1300 tokens of context
   - Function calling for state extraction
   - JSON + heuristic fallback parsing

2. **prompt_builder.py** - Monolithic prompt construction
   - Full diagnostic context in single prompt
   - All 6 phases handled by one prompt template

3. **state_tracker.py** - Heuristic state tracking fallback
   - Used when function calling/JSON parsing failed

4. **function_schemas.py** - Function calling schemas
   - Diagnostic state update function definitions

### Why Archived

**Token Efficiency:** The monolithic approach used ~1300 tokens per turn, with significant context waste:
- Phases 0-2 saw irrelevant hypothesis/solution context
- Phase 5 carried unused intake/blast radius context
- 51% of prompt tokens were not relevant to current phase

**Replaced With:** Sub-agent orchestrator using Anthropic's context engineering:
- 6 specialized phase agents with focused context
- 300-700 tokens per agent (49% reduction)
- Goal-oriented phase advancement
- Clean separation of concerns

### Migration Path

The monolithic architecture is fully replaced by:
- `orchestrator_integration.py` - Drop-in replacement adapter
- `sub_agents/orchestrator.py` - Phase routing logic
- `sub_agents/{phase}_agent.py` - 6 specialized agents

### Performance Comparison

| Metric | Monolithic | Sub-Agent | Improvement |
|--------|-----------|-----------|-------------|
| Avg tokens/turn | 1300 | 517 | 49% reduction |
| Context waste | High (51%) | Low (0-10%) | Minimal waste |
| Phase transition | Implicit | Explicit | Better control |
| Test coverage | 581 lines | 2300+ lines | 4x better |

## Rollback Instructions

If you need to rollback to monolithic architecture:

1. Restore files from this archive to `faultmaven/services/agentic/doctor_patient/`
2. Modify `agent_service.py` line 232:
   ```python
   # Change this:
   from faultmaven.services.agentic.doctor_patient.orchestrator_integration import process_turn_with_orchestrator

   # Back to this:
   from faultmaven.services.agentic.doctor_patient.turn_processor import process_turn
   ```
3. Restart backend

## References

- **Original Implementation:** DOCTOR_PATIENT_IMPLEMENTATION_SUMMARY.md
- **New Architecture:** docs/architecture/SUB_AGENT_ARCHITECTURE.md
- **Context Engineering:** docs/architecture/CONTEXT_ENGINEERING_ANALYSIS.md
