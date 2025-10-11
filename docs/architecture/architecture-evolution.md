# FaultMaven Architecture Evolution

**Last Updated:** 2025-10-05

## Current Architecture: Doctor/Patient Prompting Model ✅

**Status:** Implemented and Active
**Document:** [DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md](DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md)

### Overview

FaultMaven uses a **revolutionary single-LLM architecture** that eliminates traditional query classification. The system operates like a diagnostic doctor maintaining an internal troubleshooting agenda while naturally answering any user question.

### Key Characteristics

- **Single Powerful LLM**: No classification layer - one LLM handles everything
- **Server-Side State**: `CaseDiagnosticState` tracks SRE diagnostic progress invisibly
- **Function Calling**: 99.5% reliable state updates via native LLM function calling
- **Natural Conversation**: Users can ask anything; system maintains diagnostic flow internally
- **Adaptive Guidance**: LLM actively suggests next steps via suggested actions/commands
- **"Don't Assume Illness"**: Respects informational queries vs. troubleshooting intent

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    User Query                           │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  Turn Processor                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │ 1. Load Case Diagnostic State (server-side)    │   │
│  │ 2. Format conversation history + state context │   │
│  │ 3. Build prompt (standard/minimal/detailed)    │   │
│  └─────────────────────────────────────────────────┘   │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  Single Powerful LLM (GPT-4 / Claude 3 Opus/Sonnet)    │
│  ┌─────────────────────────────────────────────────┐   │
│  │ Prompt: Doctor/Patient SRE diagnostic guidance  │   │
│  │ Tools: KB search, web search, user docs        │   │
│  │ Function: update_diagnostic_state()            │   │
│  └─────────────────────────────────────────────────┘   │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  LLM Response                                           │
│  ┌─────────────────────────────────────────────────┐   │
│  │ • Natural answer to user's question             │   │
│  │ • Optional: Clarifying questions                │   │
│  │ • Optional: Suggested actions (clickable)       │   │
│  │ • Optional: Diagnostic commands to run          │   │
│  └─────────────────────────────────────────────────┘   │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  State Extraction (Three-Tier Fallback)                │
│  ┌─────────────────────────────────────────────────┐   │
│  │ 1. Function Calling (99.5% reliable) ✅         │   │
│  │ 2. JSON Parsing (98% reliable) 🟡               │   │
│  │ 3. Heuristic Keywords (70-80% reliable) ⚠️      │   │
│  └─────────────────────────────────────────────────┘   │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  Updated Diagnostic State (Persisted)                  │
│  ┌─────────────────────────────────────────────────┐   │
│  │ • has_active_problem: bool                      │   │
│  │ • current_phase: 0-5 (SRE methodology)          │   │
│  │ • symptoms: List[str]                           │   │
│  │ • hypotheses: List[Hypothesis]                  │   │
│  │ • solution_proposed: bool                       │   │
│  │ • case_resolved: bool                           │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### SRE 5-Phase Methodology (Internal Only - Never Announced)

The system tracks diagnostic progression through these phases server-side:

0. **Intake**: Capture problem statement, detect if active issue exists
1. **Blast Radius**: Define scope (affected users, services, regions)
2. **Timeline**: Establish when issue started, identify triggering events
3. **Hypothesis**: Formulate 2-3 ranked theories about root cause
4. **Validation**: Test hypotheses with evidence (logs, metrics, commands)
5. **Solution**: Propose remediation with implementation steps

**Critical:** Phases are never mentioned to users. Conversation flows naturally while LLM maintains this structure internally.

---

## Historical Architecture: Query Classification System v3.0

**Status:** ⚠️ SUPERSEDED (Never deployed to production)
**Document:** [QUERY_CLASSIFICATION_AND_PROMPT_ENGINEERING.md](QUERY_CLASSIFICATION_AND_PROMPT_ENGINEERING.md)

### Why It Was Abandoned

1. **Over-engineering**: 17 intent categories with complex weighted pattern matching
2. **Misclassification Risk**: "hello" triggering troubleshooting mode, rigid intent boundaries
3. **Dual-LLM Complexity**: Cheap classifier + powerful responder = unnecessary overhead
4. **Rigid User Journey**: Assumed linear progression through troubleshooting phases
5. **Multiple Competing Systems**: Boundary types, response types, intents creating confusion

### What We Learned

The classification system was **architecturally sound** and fully implemented (28/28 tests passing), but it solved the wrong problem. Key insights:

- **Modern LLMs don't need pre-classification**: GPT-4 and Claude 3 can handle intent detection, response formatting, and diagnostic reasoning simultaneously
- **Natural conversation > rigid modes**: Users want to ask anything, not be forced into troubleshooting mode
- **Internal state > external structure**: Better to track diagnostic progress server-side than enforce it turn-by-turn
- **Simplicity wins**: Single LLM with good prompting beats complex multi-stage pipelines

---

## Migration Summary

### What Changed

| Aspect | Old (Classification v3.0) | New (Doctor/Patient) |
|--------|---------------------------|----------------------|
| **LLM Architecture** | Cheap classifier + Powerful responder | Single powerful LLM |
| **Query Processing** | 17-intent classification → ResponseType → Prompt | Direct prompt with diagnostic state |
| **State Tracking** | Turn-by-turn mode switching | Continuous server-side diagnostic state |
| **User Experience** | Rigid intent-driven responses | Natural conversation with internal agenda |
| **Misclassification Risk** | High (e.g., "hello" → troubleshooting) | None (LLM handles everything) |
| **Prompt System** | Multiple: classification, boundary, response type | Single: doctor/patient SRE guidance |
| **Code Complexity** | ~3,000 lines (classification engine + tests) | ~800 lines (turn processor + state extraction) |

### What Was Kept

- ✅ **Session/Case Models**: Existing persistence infrastructure unchanged
- ✅ **Context Summarization**: Token optimization via conversation history condensation
- ✅ **LLM Provider Abstraction**: Router pattern with Fireworks/OpenAI/Anthropic support
- ✅ **Tool Integration**: Knowledge base, web search, user document search
- ✅ **PII Redaction**: Presidio-based sanitization still active

### What Was Removed

- ❌ **QueryIntent Enum**: 17 intent categories no longer needed
- ❌ **Classification Engine**: Pattern matching, confidence scoring, LLM classification
- ❌ **ResponseType Selection**: Intent-to-ResponseType mapping logic
- ❌ **Boundary Types**: Escalation/confirmation boundaries (now handled naturally by LLM)
- ❌ **28 Classification Tests**: Entire test suite for obsolete system

---

## Implementation Timeline

### 2025-10-03: Classification v3.0 Completed
- Designed response-format-driven 17-intent taxonomy
- Implemented weighted pattern matching with exclusion rules
- Built 28 comprehensive tests (100% passing)
- Never deployed to production

### 2025-10-05: Doctor/Patient Architecture Implemented
- Designed paradigm shift from classification to conversational model
- Implemented `CaseDiagnosticState` server-side tracking
- Built three prompt versions (minimal, standard, detailed)
- Integrated function calling for state extraction
- Added runbook creation on case resolution
- **Replaced** classification system in agent_service.py

### Current Status (2025-10-05)
- ✅ Core implementation complete
- 🟡 Testing in progress
- ⚠️ Case closure workflows pending
- ⚠️ Browser extension integration pending (suggested actions UI)

---

## Design Principles (Current Architecture)

### 1. Answer First, Guide Second
Always address the user's immediate question before advancing diagnostic agenda.

**Bad:** "Let's focus on your problem. Describe the symptoms."
**Good:** "Redis offers persistence and more data structures. What brings you here today?"

### 2. No Methodology Announcement
Never mention SRE phases, troubleshooting methodology, or internal processes to users.

**Bad:** "Let's move to Phase 2: Timeline Establishment."
**Good:** "When did you first notice this issue?"

### 3. Don't Assume Illness
Respect informational queries. Not every question indicates a problem.

- Listen for problem signals: "not working", "error", "failed", "down"
- Offer help, don't force troubleshooting mode
- Provide educational answers when appropriate

### 4. Active Guidance
LLM actively suggests next steps via suggested actions (clickable buttons).

Example:
```
FaultMaven: "Redis offers persistence..."

[Suggested Actions:]
• 🔧 I have a Redis issue
• 💡 Just learning
• 🏗️ Choosing for a project
```

### 5. Tool Transparency
Use knowledge base, web search, and user docs automatically without announcing tool invocation.

**Bad:** "Let me search the knowledge base... [pause] ... Here's what I found..."
**Good:** "I found a similar case - it was caused by connection pool exhaustion. Here's how to diagnose..."

---

## Future Roadmap

### Short-Term (Next 2-4 weeks)
- [ ] Comprehensive testing suite for doctor/patient system
- [ ] Case closure detection and workflow automation
- [ ] Browser extension update for suggested actions UI
- [ ] Runbook storage and retrieval (Phase 2)

### Medium-Term (1-3 months)
- [ ] Symptom clustering and timeline prioritization
- [ ] Summary report generation for closed cases
- [ ] Multi-case session management improvements
- [ ] A/B testing vs. classification baseline (if needed)

### Long-Term (3-6 months)
- [ ] Predictive phase jumping based on historical data
- [ ] Interactive diagnostic visualizations
- [ ] Automated root cause analysis from resolved cases
- [ ] Integration with monitoring systems (auto-populate state)

---

## References

- **Current Implementation**: [DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md](DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md)
- **Historical Design**: [QUERY_CLASSIFICATION_AND_PROMPT_ENGINEERING.md](QUERY_CLASSIFICATION_AND_PROMPT_ENGINEERING.md)
- **Future Enhancements**: [../FUTURE_ENHANCEMENTS.md](../FUTURE_ENHANCEMENTS.md)
- **Master Roadmap**: [../IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md)
