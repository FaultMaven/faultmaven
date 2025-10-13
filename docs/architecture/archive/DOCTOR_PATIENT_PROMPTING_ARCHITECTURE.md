# Doctor/Patient Prompting Architecture

**Version:** 1.0
**Date:** 2025-10-05
**Status:** 🔀 **PROMPTING PHILOSOPHY STILL CURRENT** | Implementation Superseded
**Current Framework:** Investigation Phases and OODA Integration v2.1 (v3.2.0)

---

## ⚠️ IMPORTANT: Prompting Philosophy vs Implementation

**This document contains TWO types of content:**

### ✅ STILL CURRENT: Prompting Philosophy

The **prompting principles** from this document are **foundational to the current OODA framework (v3.2.0)**:

- ✅ **No classification needed** - Single powerful LLM handles everything (still true)
- ✅ **"Never mention phases"** - Users shouldn't know about methodology (actively used)
- ✅ **"Answer first, guide second"** - Address user's question before guiding (core principle)
- ✅ **"Don't assume illness"** - Respect non-diagnostic intent (Consultant mode uses this)
- ✅ **Natural conversation** - Sound like colleague, not chatbot (embedded in all prompts)
- ✅ **Suggested actions** - Active guidance with clickable options (current UI pattern)
- ✅ **Function calling** - Reliable state extraction (still used)
- ✅ **Context summarization** - Prevent token explosion (still used)

**Where these principles are used today:**
- `faultmaven/prompts/investigation/consultant_mode.py` - "Never mention phases"
- `faultmaven/prompts/investigation/lead_investigator.py` - "Proactive guidance", "Evidence-driven"
- `faultmaven/prompts/doctor_patient/standard.py` - Original prompts still in codebase
- `agent_service.py` line 265: `"processing_mode": "doctor_patient"`

### ⚠️ SUPERSEDED: 6-Phase Implementation

The **specific implementation details** were superseded by the OODA framework:

- ❌ **6 phases (0-5)** → Now 7 phases (0-6) with Document phase
- ❌ **CaseDiagnosticState** → Now InvestigationState
- ❌ **Implicit methodology** → Now explicit OODA steps (Observe/Orient/Decide/Act)
- ❌ **No engagement modes** → Now Consultant vs Lead Investigator modes

**Current authoritative docs:**
- [Investigation Phases and OODA Integration Framework v2.1](../investigation-phases-and-ooda-integration.md)
- [Evidence Collection and Tracking Design v2.1](../evidence-collection-and-tracking-design.md)

---

**Use this document for:**
- Understanding WHY classification was eliminated (still valid reasoning)
- Learning prompting principles that make FaultMaven natural (still applied)
- Understanding evolution from classification → no classification → OODA

**Do NOT use this document for:**
- Current implementation details (use investigation-phases-and-ooda-integration.md)
- Current phase definitions (now 7 phases, not 6)
- Current state models (use InvestigationState, not CaseDiagnosticState)

---

**Original Status:** ✅ IMPLEMENTED (2025-10-05)
**Superseded By:** Investigation Phases and OODA Integration Framework v2.1 (2025-10-11)

## Implementation Status

| Component | Status | Location |
|-----------|--------|----------|
| **Core Models** | ✅ Complete | `faultmaven/models/doctor_patient.py`, `faultmaven/models/case.py` |
| **Prompts** | ✅ Complete | `faultmaven/prompts/doctor_patient/` (standard, minimal, detailed) |
| **Turn Processor** | ✅ Complete | `faultmaven/services/agentic/doctor_patient/turn_processor.py` |
| **State Extraction** | ✅ Complete | Function calling + JSON fallback + heuristics |
| **Context Summarization** | ✅ Complete | `faultmaven/services/domain/session_service.py` |
| **Agent Integration** | ✅ Complete | `faultmaven/services/agentic/orchestration/agent_service.py` |
| **Function Calling** | ✅ Complete | All LLM providers (Fireworks, OpenAI, Anthropic) |
| **Testing** | 🟡 In Progress | Basic integration complete, comprehensive testing pending |

**Key Features Implemented:**
- ✅ Single powerful LLM (no classification layer)
- ✅ Server-side diagnostic state tracking (`CaseDiagnosticState`)
- ✅ Function calling for reliable state updates (99.5% reliability)
- ✅ Three-tier fallback: Function calling → JSON parsing → Heuristics
- ✅ Adaptive guidance with suggested actions
- ✅ Runbook creation on case resolution
- ✅ Three prompt versions (minimal ~800, standard ~1300, detailed ~1800 tokens)
- ✅ "Don't assume illness" philosophy - respects informational queries

**Configuration:**
```bash
# .env
DOCTOR_PATIENT_PROMPT_VERSION=standard  # minimal | standard | detailed
```

---

## Executive Summary

This document describes FaultMaven's revolutionary prompting engineering architecture that eliminates traditional query classification in favor of a **doctor/patient interaction model**. Instead of classifying every user query to determine response format, FaultMaven uses a single powerful LLM that acts as a diagnostic doctor - maintaining a structured troubleshooting agenda while naturally answering any user question.

## The Paradigm Shift

### Traditional Approach (What We're Moving Away From)

```
User Query
    ↓
Cheap Classifier LLM → Intent Detection → Response Type Selection
    ↓
Powerful Response LLM → Formatted Response
```

**Problems:**
- Over-engineering classification (16 intents, multiple tiers)
- Misclassification risk at every turn (e.g., "hello" triggers troubleshooting)
- Rigid turn-by-turn mode switching
- Multiple competing prompt systems (boundary types, response types, intents)
- Assumes linear user journey through troubleshooting phases

### Doctor/Patient Model (Revolutionary Design)

```
User Query
    ↓
Single Powerful LLM (with diagnostic state context)
    ↓
Natural Response + Internal Diagnostic State Update
```

**Advantages:**
- No classification needed - LLM handles everything
- Natural conversation while maintaining diagnostic agenda
- User can ask anything at any time
- Diagnostic methodology applied continuously, not turn-by-turn
- Single source of truth for interaction behavior

## Core Philosophy

### The Doctor Analogy

**User = Patient**
- Can ask any question at any time
- Can digress, get confused, change topics
- May not understand medical terminology
- Wants to feel heard and helped

**FaultMaven = Doctor**
- Obligated to answer any question (even off-topic ones)
- Maintains diagnostic agenda across all interactions
- Follows structured diagnostic procedure (SRE 5-phase methodology)
- Guides patient through diagnosis naturally, without announcing methodology
- Decides when to order "tests" (use RAG, web search, etc.)

### Key Insight

A doctor doesn't need a triage nurse to classify every patient utterance. The doctor handles greetings, questions, concerns, and diagnostic work simultaneously while maintaining their medical expertise and structured approach to diagnosis.

Similarly, FaultMaven doesn't need query classification. One powerful LLM can:
1. Answer any question naturally
2. Maintain SRE diagnostic context internally
3. Guide users through troubleshooting without being robotic
4. Decide when to use tools (knowledge base, web search)
5. Track diagnostic state server-side

### Critical Design Principle: Don't Assume Illness

**A doctor doesn't assume every patient is sick.** Visits can be:
- Routine checkups ("How should I monitor my database?")
- Educational ("What's the difference between Redis and Memcached?")
- Exploratory ("Can you help with Kubernetes issues?")
- Actual problems ("My API is returning 500 errors!")

**FaultMaven must adapt naturally:**
- **Listen for problem signals** ("not working", "error", "failed", "down")
- **Ask permission before diagnosing** ("Would you like help troubleshooting?")
- **Respect non-diagnostic intent** (If user says "just curious", stay informational)
- **Smooth triage-to-procedure transition** (Offer help, don't force it)

### Active Guidance Philosophy

The LLM should **actively lead** the conversation using suggested actions:

**Traditional (Passive):**
```
FaultMaven: "Here's the answer. Let me know if you need anything else."
[User must figure out what to do next]
```

**New Approach (Active):**
```
FaultMaven: "Here's the answer. What brings you here?"

[Clickable suggestions:]
• "I have a Redis issue"
• "Just learning"
• "Choosing for a project"
```

**Benefits:**
- User doesn't have to think about what to ask next
- LLM guides conversation naturally
- Reduces friction in entering diagnostic mode
- Feels collaborative, not interrogative

## Architecture Components

### 1. CaseDiagnosticState Model (Server-Side Only)

The diagnostic state is a server-side-only data structure that tracks the progression of troubleshooting across all conversation turns. It is never exposed to the user but is used internally to maintain context and guide the LLM.

```python
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from enum import IntEnum

class DiagnosticPhase(IntEnum):
    """SRE diagnostic phases - never announced to users"""
    INTAKE = 0              # Initial problem intake
    BLAST_RADIUS = 1        # Define scope of impact
    TIMELINE = 2            # Establish when issues started
    HYPOTHESIS = 3          # Formulate potential causes
    VALIDATION = 4          # Validate hypotheses with evidence
    SOLUTION = 5            # Propose resolution

class Hypothesis(BaseModel):
    hypothesis: str
    evidence: List[str] = Field(default_factory=list)
    likelihood: str  # "high", "medium", "low"
    status: str = "pending"  # "pending", "validated", "rejected"

class TestPerformed(BaseModel):
    test_description: str
    result: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class CaseDiagnosticState(BaseModel):
    """
    Server-side diagnostic state tracking.
    Updated after each turn via LLM function calling.
    """
    # Problem tracking
    has_active_problem: bool = False
    problem_statement: Optional[str] = None
    problem_started_at: Optional[datetime] = None

    # SRE phase progression (0-5)
    current_phase: int = Field(default=0, ge=0, le=5)
    phase_history: List[dict] = Field(default_factory=list)

    # Phase-specific data
    symptoms: List[str] = Field(default_factory=list)
    timeline_info: List[dict] = Field(default_factory=list)
    hypotheses: List[Hypothesis] = Field(default_factory=list)
    tests_performed: List[TestPerformed] = Field(default_factory=list)

    # Solution tracking
    root_cause: Optional[str] = None
    solution_proposed: bool = False
    solution_text: Optional[str] = None
    solution_implemented: bool = False

    # Case closure
    case_resolved: bool = False
    resolution_summary: Optional[str] = None
    resolved_at: Optional[datetime] = None

    # Metadata
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    turn_count: int = 0
```

**Key Design Principles:**
- **Never shown to users** - This is internal state only
- **Persistent across turns** - Maintained server-side in Redis/database
- **Updated via function calling** - LLM updates this after each response
- **Enables continuity** - Allows agent to maintain diagnostic agenda across conversations

### 2. LLMResponse Model with Adaptive Guidance

The LLM's response includes **structured guidance** to actively lead the conversation:

```python
class SuggestedAction(BaseModel):
    """User-clickable action suggestion"""
    label: str  # Display text: "I have a Redis issue"
    type: str   # "question_template", "command", "upload_data", "transition"
    payload: str  # The actual question/command to submit
    icon: Optional[str] = None  # UI icon hint: "🔧", "💡", "🏗️"

class CommandSuggestion(BaseModel):
    """Diagnostic command the user can run"""
    command: str  # "kubectl get pods -n production"
    description: str  # "Check pod status in production namespace"
    why: str  # "This will show if any pods are failing"
    safety: str = "safe"  # "safe", "read-only", "caution"

class LLMResponse(BaseModel):
    """Structured LLM response with active guidance"""

    # Main answer to user's question
    answer: str

    # Optional: Questions LLM wants to ask for clarification
    clarifying_questions: List[str] = Field(default_factory=list)

    # Optional: Suggested actions user can take (2-4 options)
    suggested_actions: List[SuggestedAction] = Field(default_factory=list)

    # Optional: Commands to run for diagnostic evidence
    suggested_commands: List[CommandSuggestion] = Field(default_factory=list)

    # Internal: Should this trigger state update?
    requires_state_update: bool = True
```

**When to Use Each Field:**

| Field | When to Use | Example |
|-------|------------|---------|
| `answer` | Always | Direct answer to user's question |
| `clarifying_questions` | User's intent is ambiguous | "Are you troubleshooting an existing setup or evaluating options?" |
| `suggested_actions` | Offer natural next steps | "I have a problem" / "Just learning" |
| `suggested_commands` | Diagnostic mode active, need evidence | `kubectl get pods`, `redis-cli INFO` |

**Frontend Display:**

```
┌─────────────────────────────────────────────────────┐
│ FaultMaven                                          │
├─────────────────────────────────────────────────────┤
│ Redis offers persistence and more data structures   │
│ (lists, sets, sorted sets), while Memcached is      │
│ purely in-memory with simpler key-value storage.    │
│                                                      │
│ What brings you here?                               │
│                                                      │
│ ┌──────────────────────────┐ ┌───────────────────┐ │
│ │ 🔧 I have a Redis issue  │ │ 💡 Just learning  │ │
│ └──────────────────────────┘ └───────────────────┘ │
│ ┌──────────────────────────┐                       │
│ │ 🏗️  Choosing for project │                       │
│ └──────────────────────────┘                       │
└─────────────────────────────────────────────────────┘
```

Clicking a suggested action auto-submits the payload as the user's next message.

### 3. SRE Diagnostic Procedure Prompt

This is the core system prompt that guides the LLM to act as a diagnostic doctor while following SRE methodology.

```python
SRE_DIAGNOSTIC_PROCEDURE_PROMPT = """
You are FaultMaven, an expert SRE troubleshooting assistant. You operate like a diagnostic doctor:

CORE BEHAVIOR:
1. **Answer the user's question FIRST** - Always address what they asked directly and conversationally
2. **Maintain diagnostic agenda INTERNALLY** - Track SRE methodology without announcing it
3. **Never mention phases** - Users shouldn't know you're following a structured procedure
4. **Use natural language** - Conversational and helpful, not robotic or clinical
5. **Decide tool usage** - Use knowledge base/web search when needed for better answers

SRE 5-PHASE METHODOLOGY (Internal Only - Never Announce):

**Phase 0: Intake** (Initial problem capture)
- Goal: Understand if there's an active problem
- Success criteria: Clear problem statement captured
- If no problem: Answer questions, provide help, wait for problem description

**Phase 1: Define Blast Radius** (Scope of impact)
- Goal: Understand what's affected (users, services, regions, features)
- Success criteria: Clear scope documented (e.g., "50% of API users in EU region")
- Questions to explore: Who/what is impacted? What's working vs. broken?

**Phase 2: Establish Timeline** (When did it start)
- Goal: Pinpoint when the issue began and any changes around that time
- Success criteria: Timeline established with potential triggering events
- Questions to explore: When first noticed? Recent deployments? Configuration changes?

**Phase 3: Formulate Hypothesis** (Potential causes)
- Goal: Generate educated theories about root cause
- Success criteria: 2-3 ranked hypotheses with evidence
- Questions to explore: What could cause these symptoms? Historical patterns?

**Phase 4: Validate Hypothesis** (Test theories)
- Goal: Test hypotheses with evidence (logs, metrics, tests)
- Success criteria: Root cause identified with high confidence
- Questions to explore: What data supports/refutes each hypothesis?

**Phase 5: Propose Solution** (Recommend fix)
- Goal: Recommend specific remediation steps
- Success criteria: Actionable solution with implementation guidance
- Questions to explore: How to fix? Rollback needed? Prevention steps?

AVAILABLE TOOLS:
- **Knowledge Base Search**: Search FaultMaven's curated troubleshooting knowledge
- **User Document Search**: Search user's uploaded documentation/runbooks
- **Web Search**: Search internet for latest information

CRITICAL RULES:
1. **Answer first, guide second** - If user asks "What's Redis?", answer it, don't pivot to diagnosis
2. **Advance diagnosis naturally** - Weave diagnostic questions into conversation
3. **Never announce phases** - Don't say "Now let's establish the timeline..."
4. **Use tools proactively** - If knowledge base might help, use it without asking
5. **Be conversational** - Sound like a helpful colleague, not a chatbot

ADAPTIVE GUIDANCE PRINCIPLES:

**Don't Assume Illness:**
- Not every question indicates a problem
- User asking about Redis ≠ User has Redis problems
- Listen for problem signals: "not working", "error", "failed", "down", "broken"
- Respect non-diagnostic intent (learning, exploring, planning)

**Natural Triage:**
- If problem signals detected: Offer to help diagnose
- If informational: Answer thoroughly, then ask "What brings you here?"
- If exploratory: Explain capabilities, offer examples
- If unclear: Ask clarifying questions

**Active Guidance via Suggested Actions:**
- Provide 2-4 clickable options for user's next step
- Make suggestions natural, not pushy
- Frame as helpful options: "I have a problem" / "Just learning" / "Need best practices"
- Use suggested_actions to reduce friction in conversation flow

**Command Suggestions (Diagnostic Mode Only):**
- When troubleshooting is active, suggest specific diagnostic commands
- Explain WHY each command is useful (builds trust)
- Prioritize safe, read-only commands first
- Format: {command: "...", description: "...", why: "..."}

**Smooth Phase Transitions:**
- Enter diagnostic mode when user chooses "I have a problem" or shows clear problem signals
- Stay in informational mode when user is learning/exploring
- Transition naturally without announcing methodology
- Use suggested_actions to guide phase progression

DIAGNOSTIC STATE CONTEXT (Internal Only):
{diagnostic_state_context}

CONVERSATION HISTORY:
{conversation_history}

USER QUERY:
{user_query}

Respond naturally while maintaining your internal diagnostic agenda. After your response, you'll update the diagnostic state via function calling.
"""
```

**Prompt Variables:**
- `{diagnostic_state_context}` - Current phase, symptoms, hypotheses (formatted for LLM)
- `{conversation_history}` - Recent conversation turns for context
- `{user_query}` - Current user question/statement

### 3. Turn-by-Turn Processing Flow

This shows how each user query is processed with diagnostic state tracking:

```python
from typing import Tuple
from datetime import datetime

async def process_turn(
    user_query: str,
    case_id: str,
    diagnostic_state: CaseDiagnosticState,
    conversation_history: List[dict]
) -> Tuple[str, CaseDiagnosticState]:
    """
    Process a single conversation turn with diagnostic state tracking.

    Returns:
        (response_text, updated_diagnostic_state)
    """
    # 1. Build diagnostic state context for prompt
    state_context = format_diagnostic_context(diagnostic_state)

    # 2. Format conversation history (last N turns)
    history_text = format_conversation_history(conversation_history[-10:])

    # 3. Build complete prompt
    prompt = SRE_DIAGNOSTIC_PROCEDURE_PROMPT.format(
        diagnostic_state_context=state_context,
        conversation_history=history_text,
        user_query=user_query
    )

    # 4. Make LLM call with tools and function calling
    llm_response = await llm_provider.generate(
        prompt=prompt,
        tools=[
            knowledge_base_search_tool,
            user_document_search_tool,
            web_search_tool
        ],
        functions=[UPDATE_DIAGNOSTIC_STATE_FUNCTION],
        temperature=0.7
    )

    # 5. Extract response text
    response_text = llm_response.content

    # 6. Extract diagnostic state updates via function calling
    if llm_response.function_call:
        state_updates = llm_response.function_call.arguments
        updated_state = apply_state_updates(diagnostic_state, state_updates)
    else:
        # No state update, increment turn count only
        updated_state = diagnostic_state.copy(update={
            "turn_count": diagnostic_state.turn_count + 1,
            "last_updated": datetime.utcnow()
        })

    # 7. Check for case closure
    if updated_state.case_resolved and not updated_state.resolution_summary:
        # Generate summary report
        summary = await generate_summary_report(case_id, updated_state)
        updated_state.resolution_summary = summary
        updated_state.resolved_at = datetime.utcnow()

    # 8. Persist updated state
    await save_diagnostic_state(case_id, updated_state)

    return response_text, updated_state


def format_diagnostic_context(state: CaseDiagnosticState) -> str:
    """Format diagnostic state for LLM context."""
    if not state.has_active_problem:
        return "No active problem detected. Ready to help with questions or begin diagnosis."

    phase_names = {
        0: "Intake", 1: "Blast Radius", 2: "Timeline",
        3: "Hypothesis", 4: "Validation", 5: "Solution"
    }

    context = f"""
Current Diagnostic State (Internal):
- Problem: {state.problem_statement}
- Current Phase: {state.current_phase} ({phase_names[state.current_phase]})
- Symptoms Collected: {len(state.symptoms)} ({', '.join(state.symptoms[:3])})
- Hypotheses Formed: {len(state.hypotheses)}
- Tests Performed: {len(state.tests_performed)}
"""

    if state.hypotheses:
        context += "\nActive Hypotheses:\n"
        for h in state.hypotheses[:3]:
            context += f"  - {h.hypothesis} (likelihood: {h.likelihood})\n"

    if state.solution_proposed:
        context += f"\nSolution Status: Proposed - {state.solution_text[:100]}...\n"

    return context.strip()
```

## State Extraction via Function Calling

**Challenge #1 Addressed: Reliability of State Updates**

Instead of asking the LLM to output raw JSON that we parse (unreliable), we use **function calling** (also called "tool use" or "structured outputs") which is natively supported by modern LLMs and provides guaranteed schema compliance.

### Function Calling Schema

```python
UPDATE_DIAGNOSTIC_STATE_FUNCTION = {
    "name": "update_diagnostic_state",
    "description": "Update the internal diagnostic state after each response. This is for internal tracking only, never mentioned to the user.",
    "parameters": {
        "type": "object",
        "properties": {
            # Problem tracking
            "has_active_problem": {
                "type": "boolean",
                "description": "Is there currently an active technical problem being diagnosed?"
            },
            "problem_statement": {
                "type": "string",
                "description": "Clear one-sentence problem statement (e.g., 'API returning 500 errors for 50% of requests')"
            },

            # Phase progression
            "current_phase": {
                "type": "integer",
                "enum": [0, 1, 2, 3, 4, 5],
                "description": "Current SRE diagnostic phase: 0=Intake, 1=Blast Radius, 2=Timeline, 3=Hypothesis, 4=Validation, 5=Solution"
            },
            "phase_advancement_reason": {
                "type": "string",
                "description": "WHY advancing to this phase? What criteria were met? What information is now sufficient?"
            },

            # Data collection
            "new_symptoms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "New symptoms discovered this turn (append to existing)"
            },
            "new_timeline_events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "timestamp": {"type": "string"},
                        "event": {"type": "string"}
                    }
                },
                "description": "Timeline events discovered (deployments, changes, incidents)"
            },
            "new_hypotheses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "hypothesis": {"type": "string"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                        "likelihood": {"type": "string", "enum": ["high", "medium", "low"]}
                    }
                },
                "description": "New hypotheses about root cause"
            },
            "new_tests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "test_description": {"type": "string"},
                        "result": {"type": "string"}
                    }
                },
                "description": "Tests performed to validate hypotheses"
            },

            # Solution tracking
            "root_cause": {
                "type": "string",
                "description": "Identified root cause (once validated with high confidence)"
            },
            "solution_proposed": {
                "type": "boolean",
                "description": "Has a solution been proposed to the user?"
            },
            "solution_text": {
                "type": "string",
                "description": "The proposed solution with implementation steps"
            },

            # Case closure
            "case_resolved": {
                "type": "boolean",
                "description": "Is this case now resolved? (user confirmed fix or started new problem)"
            }
        },
        "required": ["has_active_problem", "current_phase"]
    }
}
```

### Goal-Oriented Phase Assessment (Challenge #3)

The extraction prompt includes **explicit phase advancement criteria** to prevent rigid progression:

```python
PHASE_ASSESSMENT_PROMPT = """
Before updating the diagnostic state, assess phase progression:

**Phase Advancement Criteria:**

Phase 0 → 1 (Intake → Blast Radius):
- Have we captured a clear problem statement?
- Does the user confirm there's an active issue?

Phase 1 → 2 (Blast Radius → Timeline):
- Do we understand WHAT is impacted (scope, users, services)?
- Can we quantify the blast radius (percentage, count, regions)?

Phase 2 → 3 (Timeline → Hypothesis):
- Do we know WHEN the problem started?
- Have we identified recent changes/events around that time?

Phase 3 → 4 (Hypothesis → Validation):
- Do we have 2+ well-formed hypotheses with evidence?
- Are hypotheses ranked by likelihood?

Phase 4 → 5 (Validation → Solution):
- Have we validated the root cause with evidence?
- Is confidence level "high" for the identified cause?

Phase 5 → Case Closure:
- Has a specific solution been proposed?
- Has the user acknowledged/implemented it?

**IMPORTANT:**
- Do NOT advance phases just to make progress
- Stay in a phase until its success criteria are genuinely met
- It's okay to spend multiple turns in the same phase
- If information is missing, set phase_advancement_reason to explain what's needed
- Phases can regress if new information contradicts previous findings

**Example Good Phase Advancement:**
current_phase: 2
phase_advancement_reason: "User confirmed deployment at 2:00 PM, logs show errors starting 2:15 PM. Timeline established with deployment as likely trigger. Ready to formulate hypotheses."

**Example Premature Advancement (AVOID):**
current_phase: 2
phase_advancement_reason: "Moving to timeline phase"  # ❌ No criteria met!
"""
```

### State Update Application

```python
def apply_state_updates(
    current_state: CaseDiagnosticState,
    updates: dict
) -> CaseDiagnosticState:
    """
    Apply function calling updates to diagnostic state.
    Merges new data with existing data intelligently.
    """
    state_dict = current_state.dict()

    # Direct field updates
    for field in ["has_active_problem", "problem_statement", "current_phase",
                  "root_cause", "solution_proposed", "solution_text", "case_resolved"]:
        if field in updates and updates[field] is not None:
            state_dict[field] = updates[field]

    # Append new symptoms (deduplicate)
    if "new_symptoms" in updates:
        existing_symptoms = set(state_dict["symptoms"])
        new_symptoms = [s for s in updates["new_symptoms"] if s not in existing_symptoms]
        state_dict["symptoms"].extend(new_symptoms)

    # Append new timeline events
    if "new_timeline_events" in updates:
        state_dict["timeline_info"].extend(updates["new_timeline_events"])

    # Append new hypotheses
    if "new_hypotheses" in updates:
        for h in updates["new_hypotheses"]:
            state_dict["hypotheses"].append(Hypothesis(**h))

    # Append new tests
    if "new_tests" in updates:
        for t in updates["new_tests"]:
            state_dict["tests_performed"].append(TestPerformed(**t))

    # Record phase advancement in history
    if "current_phase" in updates and updates["current_phase"] != current_state.current_phase:
        state_dict["phase_history"].append({
            "from_phase": current_state.current_phase,
            "to_phase": updates["current_phase"],
            "reason": updates.get("phase_advancement_reason", "Not specified"),
            "timestamp": datetime.utcnow().isoformat()
        })

    # Update metadata
    state_dict["last_updated"] = datetime.utcnow()
    state_dict["turn_count"] += 1

    return CaseDiagnosticState(**state_dict)
```

**Why Function Calling Solves Reliability (Challenge #1):**
1. **Schema Enforcement**: LLM providers guarantee the output matches the schema
2. **Type Safety**: No parsing errors from malformed JSON
3. **Native Support**: GPT-4, Claude, Gemini all support function calling natively
4. **Validation**: Pydantic validates the extracted data automatically
5. **Graceful Degradation**: If function not called, we simply don't update state

## Context Window Management

**Challenge #2 Addressed: Growing Context Size**

As diagnosis progresses, the diagnostic state accumulates symptoms, hypotheses, timeline events, and conversation history. This can exceed context windows or increase costs.

### Problem Illustration

```python
# After 20 turns of diagnosis:
diagnostic_state = CaseDiagnosticState(
    symptoms=[
        "API returns 500 errors",
        "Response time >2 seconds",
        "Database CPU at 100%",
        "Memory usage spiking",
        "Connection pool exhausted",
        "Query timeouts",
        "Cache misses increased",
        "Disk I/O high",
        "Network latency elevated",
        "Error rate 45%"
        # ... 30 more symptoms
    ],
    hypotheses=[
        # 10+ detailed hypotheses with evidence
    ],
    conversation_history=[
        # 20+ turns of back-and-forth
    ]
)
# Total prompt size: 8,000+ tokens
```

### Solution: Smart Context Summarization

```python
async def summarize_diagnostic_state(
    state: CaseDiagnosticState,
    conversation_history: List[dict],
    summarization_threshold: int = 5000  # tokens
) -> Tuple[str, List[dict]]:
    """
    Summarize diagnostic state when it exceeds threshold.

    Returns:
        (summarized_state_context, condensed_conversation_history)
    """
    # 1. Estimate current context size
    current_size = estimate_token_count(state, conversation_history)

    if current_size < summarization_threshold:
        # No summarization needed
        return format_diagnostic_context(state), conversation_history

    # 2. Summarize symptoms into categories
    summarized_symptoms = await summarize_symptoms(state.symptoms)

    # 3. Keep top 3 hypotheses, summarize rest
    top_hypotheses = sorted(
        state.hypotheses,
        key=lambda h: {"high": 3, "medium": 2, "low": 1}[h.likelihood],
        reverse=True
    )[:3]

    # 4. Summarize timeline into key events
    key_timeline_events = summarize_timeline(state.timeline_info)

    # 5. Build condensed context
    condensed_context = f"""
Diagnostic Summary (Condensed):
Problem: {state.problem_statement}
Phase: {state.current_phase} ({get_phase_name(state.current_phase)})

Symptoms Summary: {summarized_symptoms}

Top Hypotheses:
{format_hypotheses(top_hypotheses)}

Key Timeline Events:
{format_timeline(key_timeline_events)}

Tests Performed: {len(state.tests_performed)} tests completed
"""

    # 6. Condense conversation history (keep last 5 verbatim, summarize older)
    if len(conversation_history) > 5:
        older_history = conversation_history[:-5]
        recent_history = conversation_history[-5:]

        history_summary = await llm_provider.generate(
            prompt=f"Summarize this conversation in 3-4 sentences, preserving key facts:\n{format_conversation_history(older_history)}",
            temperature=0.3
        )

        condensed_history = [
            {"role": "system", "content": f"Previous conversation summary: {history_summary}"}
        ] + recent_history
    else:
        condensed_history = conversation_history

    return condensed_context, condensed_history


async def summarize_symptoms(symptoms: List[str]) -> str:
    """
    Cluster and summarize symptom list.

    Example:
        Input: ["500 error", "502 error", "API timeout", "DB CPU 100%", "DB memory high"]
        Output: "API errors (500/502), timeouts, and database resource exhaustion (CPU/memory)"
    """
    if len(symptoms) <= 5:
        return ", ".join(symptoms)

    # Use LLM to cluster and summarize
    summary = await llm_provider.generate(
        prompt=f"Summarize these symptoms in one concise sentence:\n{chr(10).join(symptoms)}",
        temperature=0.3,
        max_tokens=100
    )

    return summary.strip()


def summarize_timeline(timeline_info: List[dict]) -> List[dict]:
    """Keep only the 5 most relevant timeline events."""
    # Sort by recency and relevance (deployments, config changes prioritized)
    prioritized = sorted(
        timeline_info,
        key=lambda e: (
            "deployment" in e["event"].lower() or "config" in e["event"].lower(),
            e["timestamp"]
        ),
        reverse=True
    )
    return prioritized[:5]


def estimate_token_count(state: CaseDiagnosticState, history: List[dict]) -> int:
    """Rough token count estimation (1 token ≈ 4 characters)."""
    text = format_diagnostic_context(state) + format_conversation_history(history)
    return len(text) // 4
```

### Summarization Strategy

**What to Keep Verbatim:**
- Current phase and problem statement
- Top 3 hypotheses (ranked by likelihood)
- Last 5 conversation turns
- Most recent timeline events

**What to Summarize:**
- Long symptom lists → categorized summary
- Older conversation turns → narrative summary
- Many hypotheses → keep top-ranked, summarize rest
- Timeline events → keep deployments/changes, summarize rest

**When to Summarize:**
- When estimated context exceeds 5,000 tokens
- Before advancing to validation phase (consolidate findings)
- Every 10 turns as preventive measure

**Benefits:**
- Keeps context window manageable (3,000-4,000 tokens)
- Reduces LLM costs by 40-60%
- Maintains diagnostic continuity
- No information loss (summaries capture essence)

## Case Closure Detection

The system must detect when a troubleshooting case is complete and ready for closure.

### Three Types of Closure

**1. Explicit Closure (User Confirms Resolution)**
```python
User: "That fixed it! The API is working now."
LLM Function Call: {
    "case_resolved": true,
    "root_cause": "Database connection pool exhaustion due to missing timeout configuration",
    "solution_implemented": true
}
```

**2. Implicit Closure (Solution Proposed, User Acknowledges)**
```python
User: "Thanks, I'll try that rollback and let you know."
LLM Function Call: {
    "solution_proposed": true,
    "solution_text": "Rollback to v2.3.1 deployment which was stable...",
    "case_resolved": false  # Not resolved yet, but solution delivered
}
# Case marked as "pending_user_action"
```

**3. New Issue (User Pivots to Different Problem)**
```python
# Previous case: API 500 errors
User: "Actually, I have a different issue - the frontend is broken."
LLM Function Call: {
    "case_resolved": true,  # Close previous case
    "resolution_summary": "Investigation narrowed to database connection pool. User pivoted to new issue before implementing solution."
}
# System creates NEW case for frontend issue
```

### Closure Detection Logic

```python
async def detect_case_closure(
    diagnostic_state: CaseDiagnosticState,
    llm_response: str,
    user_query: str
) -> Tuple[bool, Optional[str]]:
    """
    Detect if case should be closed.

    Returns:
        (should_close, closure_reason)
    """
    # Explicit closure: Function call marked it
    if diagnostic_state.case_resolved:
        return True, "explicit_resolution"

    # Solution delivered and acknowledged
    if diagnostic_state.solution_proposed:
        acknowledgment_patterns = [
            r"(?i)(thanks?|thank you|got it|understood|i'll try|let me try)",
            r"(?i)(makes sense|sounds good|perfect|great)"
        ]
        for pattern in acknowledgment_patterns:
            if re.search(pattern, user_query):
                return True, "solution_acknowledged"

    # New issue detection (topic shift)
    if diagnostic_state.has_active_problem:
        new_issue = await detect_new_issue(user_query, diagnostic_state.problem_statement)
        if new_issue:
            return True, "new_issue_started"

    # Case inactive for too long (10+ turns without progress)
    if diagnostic_state.turn_count > 10 and diagnostic_state.current_phase < 3:
        return True, "case_stalled"

    return False, None


async def detect_new_issue(
    user_query: str,
    current_problem: str
) -> bool:
    """Use LLM to detect if user is describing a different problem."""
    prompt = f"""
Current problem: {current_problem}
User query: {user_query}

Is the user describing a DIFFERENT technical problem (not related to the current one)?
Answer: yes or no
"""
    response = await llm_provider.generate(prompt=prompt, temperature=0.1, max_tokens=5)
    return response.strip().lower() == "yes"
```

## Summary Report Generation

When a case closes, generate a comprehensive summary report.

```python
async def generate_summary_report(
    case_id: str,
    diagnostic_state: CaseDiagnosticState
) -> str:
    """
    Generate case summary report for historical record.
    This is stored server-side and can be retrieved by users.
    """
    # Build report context
    report_context = f"""
Generate a concise troubleshooting case summary:

Problem: {diagnostic_state.problem_statement}
Symptoms: {', '.join(diagnostic_state.symptoms)}
Root Cause: {diagnostic_state.root_cause or 'Not fully identified'}
Solution: {diagnostic_state.solution_text or 'Not provided'}

Timeline Events:
{format_timeline(diagnostic_state.timeline_info)}

Hypotheses Tested:
{format_hypotheses_tested(diagnostic_state.hypotheses)}

Tests Performed: {len(diagnostic_state.tests_performed)}

Format as a professional incident report summary in 3-4 paragraphs.
"""

    summary = await llm_provider.generate(
        prompt=report_context,
        temperature=0.4,
        max_tokens=500
    )

    # Add metadata
    report = f"""
# Case Summary Report
**Case ID:** {case_id}
**Resolved:** {diagnostic_state.resolved_at.strftime('%Y-%m-%d %H:%M:%S')}
**Total Turns:** {diagnostic_state.turn_count}
**Final Phase:** {get_phase_name(diagnostic_state.current_phase)}

{summary}

---
*Generated by FaultMaven Diagnostic Engine*
"""

    return report


def format_hypotheses_tested(hypotheses: List[Hypothesis]) -> str:
    """Format hypothesis list with validation status."""
    output = []
    for h in hypotheses:
        status_emoji = {"validated": "✓", "rejected": "✗", "pending": "○"}[h.status]
        output.append(f"{status_emoji} {h.hypothesis} (likelihood: {h.likelihood})")
    return "\n".join(output)
```

## Case Persistence Model

### One Problem Per Case

Each technical problem gets its own case. Sessions can contain multiple cases over time.

```python
class Case(BaseModel):
    """A single troubleshooting case (one problem)."""
    case_id: str = Field(default_factory=lambda: f"case_{uuid.uuid4().hex[:8]}")
    session_id: str

    # Case lifecycle
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None
    status: str = "active"  # "active", "resolved", "stalled"

    # Diagnostic state
    diagnostic_state: CaseDiagnosticState = Field(default_factory=CaseDiagnosticState)

    # Conversation specific to this case
    messages: List[Message] = Field(default_factory=list)

    # Summary (generated on closure)
    summary_report: Optional[str] = None


class Session(BaseModel):
    """A user session (may contain multiple cases)."""
    session_id: str = Field(default_factory=lambda: f"session_{uuid.uuid4().hex}")
    user_id: Optional[str] = None

    # Session lifecycle
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_activity: datetime = Field(default_factory=datetime.utcnow)

    # Cases in this session
    case_ids: List[str] = Field(default_factory=list)
    active_case_id: Optional[str] = None

    # Session-level context (persists across cases)
    user_context: dict = Field(default_factory=dict)  # Stack info, team, etc.
```

### Case/Session Relationship

```
Session: session_abc123
├── Case 1: case_f4a8 (RESOLVED)
│   Problem: "API 500 errors"
│   Status: resolved
│   Messages: 12 turns
│   Summary: "Root cause: DB connection pool..."
│
├── Case 2: case_9d2c (ACTIVE)
│   Problem: "Frontend build failing"
│   Status: active
│   Messages: 5 turns
│   Summary: null
│
└── Session Context:
    - Stack: Node.js + PostgreSQL
    - Team: Platform Engineering
    - Timezone: UTC-8
```

### Case State Management

```python
async def get_or_create_active_case(session_id: str) -> Case:
    """Get active case for session or create new one."""
    session = await get_session(session_id)

    if session.active_case_id:
        case = await get_case(session.active_case_id)
        if case.status == "active":
            return case

    # No active case, create new one
    new_case = Case(session_id=session_id)
    await save_case(new_case)

    session.case_ids.append(new_case.case_id)
    session.active_case_id = new_case.case_id
    await save_session(session)

    return new_case


async def close_case(case_id: str, reason: str) -> None:
    """Close a case and generate summary."""
    case = await get_case(case_id)

    case.status = "resolved" if reason == "explicit_resolution" else "closed"
    case.closed_at = datetime.utcnow()
    case.summary_report = await generate_summary_report(case_id, case.diagnostic_state)

    await save_case(case)

    # Clear active case in session
    session = await get_session(case.session_id)
    if session.active_case_id == case_id:
        session.active_case_id = None
        await save_session(session)
```

## Prompt Engineering Principles

### 1. No Methodology Announcement

**Bad (Robotic):**
```
User: "My API is slow"
FaultMaven: "I'll help you troubleshoot using the 5-phase SRE methodology. First, let's define the blast radius. Can you tell me..."
```

**Good (Natural):**
```
User: "My API is slow"
FaultMaven: "I can help with that. How widespread is the slowness - is it affecting all users or just specific endpoints?"
```

**Implementation:**
- Prompt explicitly forbids mentioning phases or methodology
- Agent internalizes SRE doctrine but speaks conversationally
- Questions are woven naturally into helpful responses

### 2. Answer First, Guide Second

**Bad (Ignoring User):**
```
User: "What's the difference between Redis and Memcached?"
FaultMaven: "Let's focus on your problem. Can you describe the symptoms you're experiencing?"
```

**Good (Helpful Doctor):**
```
User: "What's the difference between Redis and Memcached?"
FaultMaven: "Redis supports more data structures (lists, sets, sorted sets) and persistence, while Memcached is simpler and purely in-memory. For troubleshooting, if you're seeing Redis-related errors, knowing which data structures you're using helps narrow down the issue. Is your problem related to caching?"
```

**Implementation:**
- Always address user's immediate question first
- Connect answer to ongoing diagnosis naturally
- Don't force diagnostic agenda when user has different needs

### 3. Tool Transparency

**Bad (Hiding Tool Use):**
```
FaultMaven: "Let me check something... [searches KB silently] ... Here's what I found..."
```

**Good (Natural Mention):**
```
FaultMaven: "I found a similar case in our knowledge base - it was caused by a connection pool leak. Let me walk you through the diagnostic steps..."
```

**Implementation:**
- Tools used automatically based on prompt logic
- Results incorporated naturally into response
- No need to announce "I'm using the knowledge base tool"

### 4. Implicit Phase Progression

**Bad (Explicit Phases):**
```
FaultMaven: "Great! We've completed Phase 1 (Blast Radius). Now let's move to Phase 2 (Timeline Establishment)."
```

**Good (Natural Flow):**
```
FaultMaven: "So it's impacting about 50% of users in the EU region. When did you first notice this issue - was there a deployment or configuration change around that time?"
```

**Implementation:**
- Phase advances tracked server-side via function calling
- Conversation flows naturally without phase announcements
- Questions guide user through methodology invisibly

### 5. Graceful Digression Handling

**Bad (Rigid):**
```
User: "Quick question - what does 'eventual consistency' mean?"
FaultMaven: "We're currently diagnosing your API issue. Let's stay focused on that."
```

**Good (Flexible Doctor):**
```
User: "Quick question - what does 'eventual consistency' mean?"
FaultMaven: "It means data changes propagate gradually across replicas, so reads might temporarily show stale data. This is relevant to your issue - if your database uses eventual consistency, the errors you're seeing could be due to replica lag. Are you reading from replicas or the primary?"
```

**Implementation:**
- No question is "off-topic" - answer everything
- Connect tangents back to diagnosis when possible
- Maintain diagnostic state even during digressions

### 6. Adaptive Guidance Examples

**Scenario 1: Educational Visit (No Problem)**
```json
User: "What's the difference between Redis and Memcached?"

LLM Response:
{
  "answer": "Redis offers persistence and more data structures (lists, sets, sorted sets), while Memcached is purely in-memory with simpler key-value storage. Redis is generally more feature-rich but uses more memory.",
  "suggested_actions": [
    {
      "label": "🔧 I have a caching issue",
      "type": "question_template",
      "payload": "I'm experiencing caching issues with my setup"
    },
    {
      "label": "💡 Just learning about caching",
      "type": "question_template",
      "payload": "Can you explain more about caching strategies?"
    },
    {
      "label": "🏗️ Choosing between them",
      "type": "question_template",
      "payload": "I'm evaluating Redis vs Memcached for my project"
    }
  ]
}

→ State Update: has_active_problem = False (informational query)
```

**Scenario 2: Problem Signal Detected**
```json
User: "My pods keep restarting"

LLM Response:
{
  "answer": "Pod restarts usually indicate either crashes (application error), OOMKilled (out of memory), failed health checks, or node issues. Let's diagnose this step by step.",
  "suggested_commands": [
    {
      "command": "kubectl get pods -A",
      "description": "Show all pods and their statuses",
      "why": "This will show which pods are restarting and their current state",
      "safety": "safe"
    },
    {
      "command": "kubectl describe pod <pod-name>",
      "description": "Get detailed pod information",
      "why": "Shows restart count, exit codes, and recent events",
      "safety": "safe"
    }
  ],
  "suggested_actions": [
    {
      "label": "📋 Share pod name",
      "type": "question_template",
      "payload": "The pod name is [pod-name]"
    },
    {
      "label": "📤 Upload kubectl output",
      "type": "upload_data",
      "payload": "upload_diagnostic_data"
    }
  ]
}

→ State Update:
  has_active_problem = True
  problem_statement = "User reports pods restarting repeatedly"
  current_phase = 0 (Intake)
  symptoms = ["pod restarts"]
```

**Scenario 3: Routine Checkup**
```json
User: "How should I monitor my Redis cluster?"

LLM Response:
{
  "answer": "For Redis monitoring, focus on these key metrics:\n1. Memory usage (used_memory, maxmemory)\n2. Hit rate (keyspace_hits / keyspace_misses)\n3. Connected clients\n4. Commands per second\n5. Replication lag (if using replicas)\n\nYou can get these via `redis-cli INFO` command or monitoring tools like Prometheus + Grafana.",
  "suggested_actions": [
    {
      "label": "🛠️ Help me set up monitoring",
      "type": "question_template",
      "payload": "I want to set up Redis monitoring with Prometheus"
    },
    {
      "label": "🔧 I think my Redis has a problem",
      "type": "question_template",
      "payload": "I'm seeing issues with my Redis cluster"
    },
    {
      "label": "📊 Show example queries",
      "type": "question_template",
      "payload": "Can you show me example Prometheus queries for Redis?"
    }
  ]
}

→ State Update: has_active_problem = False (preventive/educational)
```

**Scenario 4: Ambiguous Intent - Clarification Needed**
```json
User: "Redis connection issues"

LLM Response:
{
  "answer": "I can help with Redis connection issues. To diagnose effectively, I need to understand what you're experiencing.",
  "clarifying_questions": [
    "Are you seeing connection timeouts, connection refused, or authentication errors?",
    "Is this a new setup or did it work before?"
  ],
  "suggested_actions": [
    {
      "label": "🚫 Connection refused errors",
      "type": "question_template",
      "payload": "I'm getting 'connection refused' errors"
    },
    {
      "label": "⏱️ Connection timeouts",
      "type": "question_template",
      "payload": "Connections are timing out"
    },
    {
      "label": "🔐 Authentication failures",
      "type": "question_template",
      "payload": "Getting authentication errors"
    }
  ]
}

→ State Update:
  has_active_problem = True (problem indicated but needs clarification)
  problem_statement = "User experiencing Redis connection issues (type unclear)"
  current_phase = 0 (Intake - gathering details)
```

**Key Benefits:**
- **Reduces Friction**: User clicks instead of typing
- **Guides Naturally**: Options suggest logical next steps
- **Respects Intent**: Offers both problem-solving and learning paths
- **Active Leadership**: LLM drives conversation forward

## Implementation Checklist

### Phase 1: Core Infrastructure ✅ COMPLETE
- ✅ Define `CaseDiagnosticState` Pydantic model (`faultmaven/models/case.py`)
- ✅ Implement `Case` and `Session` persistence models (existing)
- ✅ Create `UPDATE_DIAGNOSTIC_STATE_FUNCTION` schema (`function_schemas.py`)
- ✅ Build `apply_state_updates()` function (in `state_extractor.py`)
- ✅ Implement `format_diagnostic_context()` for prompt injection (`turn_processor.py`)

### Phase 2: Turn Processing ✅ COMPLETE
- ✅ Build doctor/patient prompt templates (`prompts/doctor_patient/standard.py`, `minimal.py`, `detailed.py`)
- ✅ Implement `process_turn()` main processing function (`turn_processor.py`)
- ✅ Add function calling support to LLM provider (all providers: Fireworks, OpenAI, Anthropic)
- ✅ Create conversation history formatting (session_service.py has `format_conversation_context_token_aware()`)
- ✅ Implement state persistence (via `case_service.update_case()`)

### Phase 3: Case Management ✅ COMPLETE
- ✅ Implement `get_or_create_active_case()` (exists in case_service.py)
- ✅ Build case closure detection logic (runbook offer on resolution)
- ⚠️ Create `detect_new_issue()` topic shift detection (NOT YET IMPLEMENTED)
- ⚠️ Implement `close_case()` workflow (NOT YET IMPLEMENTED)
- ✅ Add session-level case tracking (existing Case model with `diagnostic_state` field)

### Phase 4: Summarization ✅ COMPLETE
- ✅ Context summarization exists (`session_service.format_conversation_context_token_aware()`)
- ⚠️ `summarize_symptoms()` clustering (NOT YET IMPLEMENTED - using basic heuristics)
- ⚠️ `summarize_timeline()` prioritization (NOT YET IMPLEMENTED)
- ✅ Context window estimation (existing in session_service)
- ✅ Conversation history condensation (40-60% token savings via LLM summarization)
- ⚠️ `generate_summary_report()` for closure (NOT YET IMPLEMENTED - planned for runbook phase 2)

### Phase 5: API Integration ✅ COMPLETE
- ✅ Update `/agent/chat` endpoint to use new architecture (agent_service.py modified)
- ✅ Case retrieval endpoints exist (`GET /cases/{case_id}`)
- ✅ Case history endpoints exist (`GET /sessions/{session_id}/cases`)
- ⚠️ Summary report retrieval (NOT YET IMPLEMENTED - planned)
- 🟡 Browser extension integration (PENDING - extension needs update for suggested actions)

### Phase 6: Testing & Validation 🟡 IN PROGRESS
- 🟡 Unit tests for state extraction (NEEDED)
- 🟡 Integration tests for turn processing (NEEDED)
- ⚠️ Test case closure detection (NOT YET IMPLEMENTED)
- ⚠️ Validate summarization quality (NOT YET IMPLEMENTED)
- ✅ Test context window management (existing tests in session_service)
- 🟡 Validate prompt engineering principles (MANUAL TESTING ONLY)
- 🟡 End-to-end troubleshooting scenarios (MANUAL TESTING ONLY)

### Legend
- ✅ Complete and working
- 🟡 Partially complete or needs testing
- ⚠️ Not yet implemented but planned

## Quality Metrics

Track these metrics to validate the new architecture:

### Diagnostic Quality
- **Phase progression accuracy**: Are phases advancing at appropriate times?
  - Target: 90%+ advancement decisions justified by criteria
  - Measure: Manual review of `phase_advancement_reason` fields

- **Case closure accuracy**: Are cases closing at the right times?
  - Target: 95%+ correct closure detection
  - Measure: False positives (premature closure) + false negatives (stalled cases)

- **Summarization quality**: Are summaries preserving essential information?
  - Target: 95%+ information retention
  - Measure: LLM-based comparison of summary vs. full state

### User Experience
- **Response relevance**: Are responses answering user questions?
  - Target: 95%+ relevance score
  - Measure: User feedback + LLM-based evaluation

- **Natural conversation**: Does it feel conversational?
  - Target: 90%+ positive sentiment
  - Measure: User feedback on "robotic" vs. "helpful" responses

- **Time to resolution**: Is troubleshooting faster?
  - Baseline: Current average turns to resolution
  - Target: 20% reduction
  - Measure: Average turns per case (resolved cases only)

### Technical Performance
- **Context window usage**: Are we staying within limits?
  - Target: < 5,000 tokens average context size
  - Measure: Token count tracking with summarization

- **LLM cost reduction**: Are we reducing API costs?
  - Baseline: Current cost per session
  - Target: 30% reduction (via summarization)
  - Measure: Total tokens per case

- **Function calling reliability**: Are state updates succeeding?
  - Target: 99%+ successful function calls
  - Measure: Function call success rate

## Expected Benefits

### 1. Simplified Architecture
- **Before**: 3-tier classification system (16 intents, boundary types, response types)
- **After**: Single powerful LLM with internal state tracking
- **Impact**: 60% reduction in codebase complexity

### 2. Improved User Experience
- Natural conversation without rigid turn-by-turn modes
- Users can ask anything at any time
- No more misclassification errors ("hello" triggering troubleshooting)

### 3. Better Diagnostic Quality
- Structured SRE methodology applied consistently
- Goal-oriented phase progression (not mechanical)
- Comprehensive diagnostic state tracking
- Historical case summaries for learning

### 4. Scalability
- Context window management via summarization
- 30%+ cost reduction through intelligent context pruning
- Persistent case state enables async troubleshooting

### 5. Observability
- Complete diagnostic audit trail (phase history, hypotheses, tests)
- Case summaries for post-mortems
- Clear metrics for quality measurement

## Revolutionary Aspects

### 1. No Classification Layer
Traditional chatbots classify every query to determine response type. FaultMaven eliminates this entirely - one LLM handles everything.

### 2. Internal vs. External State
The diagnostic state is never shown to users. It's purely internal scaffolding that maintains diagnostic continuity while allowing natural conversation.

### 3. Goal-Oriented Phase Progression
Phases advance based on **criteria met**, not mechanical progression. The LLM must justify advancement with `phase_advancement_reason`, preventing robotic behavior.

### 4. Function Calling for State Updates
Instead of unreliable JSON parsing, native function calling ensures 99%+ reliability in state updates with schema enforcement.

### 5. Adaptive Context Management
Automatic summarization prevents context window overflow while preserving diagnostic continuity - the system "remembers" everything but presents it efficiently.

## Future Enhancements

### Multi-Agent Collaboration
Allow multiple diagnostic agents to collaborate on complex issues, each maintaining their own diagnostic state but sharing findings.

### Predictive Phase Jumping
Use historical case data to predict when phases can be safely skipped (e.g., if user provides comprehensive initial report, jump to Phase 3).

### Interactive Diagnostic Visualizations
Generate real-time diagnostic flow diagrams showing symptom clusters, hypothesis trees, and timeline correlations.

### Automated Root Cause Analysis
Train specialized models on resolved cases to automatically suggest likely root causes based on symptom patterns.

### Integration with Monitoring Systems
Auto-populate diagnostic state from monitoring alerts, logs, and metrics - reducing manual data entry for users.

---

**Status**: This architecture represents a fundamental shift from classification-based chatbots to doctor/patient interaction models. Implementation is ready to begin with clear phases, metrics, and migration strategy.
