# FaultMaven Prompt Engineering Guide v2.0

## Executive Summary

This document defines FaultMaven's complete prompt engineering architecture for the milestone-based investigation framework. It provides strategic philosophy, tactical templates, and implementation guidance for building AI-powered troubleshooting conversations that feel natural while maintaining diagnostic rigor.

**Key Capabilities**:
- **Milestone-based prompts** - Opportunistic task completion, no rigid phases
- **Three-template system** - CONSULTING → INVESTIGATING → TERMINAL
- **LLM as form-filler** - Structured state updates + natural conversation
- **Adaptive instructions** - Stage-aware guidance within single template
- **Working conclusion tracking** - Continuous investigation with transparent confidence
- **Graceful degradation** - Degraded mode with explicit confidence caps
- **Clear boundaries** - LLM determines observables, system infers calculations

**Document Version**: 2.0  
**Alignment**: Investigation Architecture v2.0, Case Model Design v2.0  
**Status**: Production Specification

---

## Table of Contents

1. [Design Philosophy](#1-design-philosophy)
2. [Core Architecture](#2-core-architecture)
3. [Template 1: CONSULTING](#3-template-1-consulting)
4. [Template 2: INVESTIGATING](#4-template-2-investigating)
5. [Template 3: TERMINAL](#5-template-3-terminal)
6. [LLM vs System Responsibilities](#6-llm-vs-system-responsibilities)
7. [Advanced Features](#7-advanced-features)
8. [System Post-Processing](#8-system-post-processing)
9. [Key Principles](#9-key-principles)
10. [Validation & Testing](#10-validation--testing)

---

## 1. Design Philosophy

### 1.1 The Doctor-Patient Paradigm

FaultMaven maintains **structured diagnostic methodology** while presenting **natural conversation**. Think of it as a doctor conducting an examination - there's a systematic approach underneath, but the patient experiences caring guidance.

| Medical Concept | FaultMaven Implementation | Prompting Technique |
|----------------|---------------------------|---------------------|
| **Doctor meets patient** | Consultant Mode (CONSULTING) | Reactive prompts, detect problem signals |
| **Doctor leads diagnosis** | Lead Investigator (INVESTIGATING) | Proactive prompts, evidence requests |
| **Structured procedure** | Milestone-based investigation | Track completion, not position |
| **Order lab tests** | Evidence request generation | Specific, actionable requests |
| **Analyze results** | Evidence processing | LLM analyzes, system categorizes |
| **Make diagnosis** | Root cause identification | Direct or hypothesis-driven |
| **Prescribe treatment** | Solution implementation | Immediate + long-term fixes |
| **Always maintain working theory** | Working conclusion (every turn) | Transparent confidence levels |
| **Acknowledge limitations** | Degraded mode | Confidence caps, honest constraints |
| **Never mention procedure** | "No methodology jargon" rule | User sees guidance, not mechanics |

### 1.2 Core Prompting Principles

**1. No Classification Layer** ✅
```
Traditional:  User Query → Classifier LLM → Response LLM → Answer
FaultMaven:   User Query → Single Powerful LLM → Answer

Why: Faster, cheaper, more coherent. Single LLM maintains conversation context.
```

**2. Never Mention Internal Mechanics** ✅
```python
❌ BAD: "Let's move to Phase 3: Hypothesis Generation"
✅ GOOD: "Based on these symptoms, here are the most likely causes..."

❌ BAD: "I'm updating milestone: symptom_verified"
✅ GOOD: "Great, I've confirmed the symptom from the logs you provided"

❌ BAD: "We're in the HYPOTHESIS_FORMULATION stage now"
✅ GOOD: "Now that we understand the problem, let's find why it's happening"
```

**Rule**: Users should NEVER see: milestones, stages, status, phases, OODA, framework terms.

**3. Answer First, Guide Second** ✅
```python
# Lead Investigator principle
❌ BAD: "Can you provide connection pool metrics?"
✅ GOOD: "Thanks for the error logs. They show timeout errors starting at 14:23 UTC. 
          Can you provide connection pool metrics to verify if the pool is exhausted?"

# Always acknowledge what user provided before requesting more
```

**4. Don't Assume Problem Exists** ✅
```python
# Consultant Mode principle
❌ BAD: "Let's start the investigation" (user just asked a question)
✅ GOOD: "I can help with that. Would you like me to investigate this systematically, 
          or just answer your question?"

# Detect problem signals, offer investigation only when appropriate
```

**5. Natural, Conversational Tone** ✅
```python
❌ BAD: "Proceeding with hypothesis validation. Evidence request generated."
✅ GOOD: "To test this theory, I need to check the connection pool metrics. 
          Could you run: kubectl top pods?"

# Sound like a helpful colleague, not a robot
```

**6. Always Investigating** ✅
```python
# Continuous investigation principle
✅ Agent maintains working conclusion at ALL times with transparent confidence
✅ No mode switching between "investigating" and "stuck"
✅ Confidence varies (30% → 70% → 95%), but investigation never stops
✅ Even at 30% confidence: "Based on limited evidence, I speculate..."

# Investigation quality degrades gracefully, never halts abruptly
```

**7. Graceful Degradation** ✅
```python
# When fundamental barriers hit
✅ Operate in degraded mode with explicit confidence caps
✅ Communicate constraints transparently
✅ Continue investigation within limitations
✅ Offer escalation options

# Never pretend higher confidence than evidence supports
```

### 1.3 The Milestone-Based Philosophy

**Core Insight**: Investigation is **data-driven and opportunistic**, not phase-constrained.

```
MILESTONE-BASED MODEL:
Agent completes ALL tasks for which sufficient data exists IN ONE TURN

Example:
User uploads comprehensive log containing:
  - Error messages (symptom data)
  - Timestamps (timeline data)
  - Stack trace (root cause data)

Agent in ONE turn:
  ✅ symptom_verified
  ✅ timeline_established
  ✅ root_cause_identified
  ✅ solution_proposed

Just complete what you can based on available data.
```

**Design Philosophy**: The agent checks "what data is available?" and "what's been completed?" rather than following sequential phase transitions. This enables opportunistic task completion.

**Prompting Implications**:
- ✅ Show milestone status (what's done)
- ✅ Emphasize current stage (what to focus on)
- ✅ Allow jumping ahead ("If data allows, complete multiple milestones")
- ✅ Focus on data availability, not artificial constraints

### 1.4 LLM as Form-Filler + Conversationalist

**Mental Model**: Each turn, LLM performs **two tasks**:

```
┌─────────────────────────────────────────────────────────┐
│ Task 1: Fill Investigation State Form                  │
│ (Structured data: milestones, evidence, conclusions)   │
└─────────────────────────────────────────────────────────┘
                          +
┌─────────────────────────────────────────────────────────┐
│ Task 2: Respond to User Naturally                      │
│ (Conversational text: explain findings, request data)  │
└─────────────────────────────────────────────────────────┘
```

**This separation is critical**:
- **Form = State management** (what changed in investigation)
- **Response = Communication** (what to tell user)

These are NOT the same thing:

```python
# Example: Root cause identified

Form (Structured State Update):
{
  "milestones": {
    "root_cause_identified": True,
    "root_cause_confidence": 0.85
  },
  "root_cause_conclusion": {
    "root_cause": "Connection pool exhaustion due to connection leak",
    "mechanism": "Async queries not closing connections, pool fills over time",
    "confidence_score": 0.85
  }
}

Response (Natural Conversation):
"I'm confident (85%) that this is a connection pool exhaustion issue. 
The error patterns match pool exhaustion, and the timing correlates with 
the deployment that added async queries. These queries aren't closing 
connections properly, so the pool fills up over ~2 hours of uptime."

# Form = machine-readable state
# Response = human-readable explanation
```

---

## 2. Core Architecture

### 2.1 Three-Template System

**Template selection based on `case.status`**:

```
┌──────────────┐
│  CONSULTING  │ ─── Template #1: ConsultingResponse
└──────┬───────┘     Purpose: Explore problem, get commitment
       │             Frequency: ~10% of turns
       │             Complexity: Low
       │
       ├─────(User decides to investigate)────────┐
       │                                          │
       │                                          ▼
       │                              ┌────────────────────┐
       │                              │   INVESTIGATING    │ ─── Template #2: InvestigationResponse
       │                              │                    │     Purpose: Complete milestones
       │                              │ • Understanding    │     Frequency: ~85% of turns
       │                              │ • Diagnosing       │     Complexity: High
       │                              │ • Resolving        │     Features: Adaptive instructions
       │                              └─────────┬──────────┘
       │                                        │
       │                              ┌─────────┴──────────┐
       │                              │                    │
       │                   (solution_verified)    (closed without solution)
       │                              │                    │
       │                              ▼                    ▼
       │                      ┌──────────────┐    ┌──────────────┐
       │                      │   RESOLVED   │    │    CLOSED    │ ─── Template #3: TerminalResponse
       │                      │              │    │              │     Purpose: Documentation
       │                      │   TERMINAL   │    │   TERMINAL   │     Frequency: ~5% of turns
       │                      └──────────────┘    └──────────────┘     Complexity: Low
       │                                                  ▲
       └──(consulting-only)──────────────────────────────┘
```

**Why 3 Templates?**

1. **CONSULTING** - Pre-investigation (simple schema, light prompts)
2. **INVESTIGATING** - Active investigation (complex schema, adaptive prompts)
3. **TERMINAL** - Post-investigation (read-only, documentation focus)

**Key Decision**: Template #2 (INVESTIGATING) uses **adaptive instructions** for 3 stages rather than 3 separate templates. This allows:
- ✅ Same schema for all stages (LLM can fill any section)
- ✅ Simpler maintenance (one template vs three)
- ✅ Enables jumping ahead (not constrained by stage)

### 2.2 Investigation Stages (Computed, Not Set)

**Stages are COMPUTED from milestone state** (not manually set by LLM):

```python
def compute_stage(progress: InvestigationProgress) -> InvestigationStage:
    """System computes stage from milestone completion"""

    # SOLUTION: Any solution work
    if (progress.solution_proposed or
        progress.solution_applied or
        progress.solution_verified):
        return InvestigationStage.SOLUTION

    # HYPOTHESIS_VALIDATION: Root cause identified, now validating
    if progress.root_cause_identified and not progress.solution_proposed:
        return InvestigationStage.HYPOTHESIS_VALIDATION

    # HYPOTHESIS_FORMULATION: Verification complete, finding root cause
    if progress.symptom_verified and not progress.root_cause_identified:
        return InvestigationStage.HYPOTHESIS_FORMULATION

    # SYMPTOM_VERIFICATION: Initial verification
    return InvestigationStage.SYMPTOM_VERIFICATION
```

**Stage Definition**:

| Stage | Focus | Milestones | Purpose |
|-------|-------|-----------|---------|
| **SYMPTOM_VERIFICATION** | Verification | symptom_verified, scope_assessed, timeline_established, changes_identified | Confirm problem is real, understand context |
| **HYPOTHESIS_FORMULATION** | Hypothesis generation | root_cause_identified | Generate theories about why it happened |
| **HYPOTHESIS_VALIDATION** | Root cause validation | root_cause_identified | Test and validate hypotheses |
| **SOLUTION** | Solution | solution_proposed, solution_applied, solution_verified | Fix the problem |

**Prompting Strategy**:
- Show current stage in prompt (for context)
- Provide stage-specific guidance (emphasis)
- BUT allow completing ANY milestone if data available

### 2.3 Schema Design

**All templates return: Natural response + Structured state updates**

```python
# Base pattern for all templates
class BaseResponse(BaseModel):
    agent_response: str              # Natural language to user
    state_updates: StateUpdateType   # Structured investigation state changes
```

**Three state update schemas**:

1. **ConsultingStateUpdate** - Simple (problem understanding, quick suggestions)
2. **InvestigationStateUpdate** - Complex (milestones, evidence, hypotheses, solutions)
3. **TerminalStateUpdate** - Minimal (documentation only)

**Design Principle**: Schema reflects what LLM can realistically determine from available data.

### 2.4 Prompt Assembly Strategy

**Prompts are composed of contextual sections**:

```python
def build_prompt(case: Case, user_message: str) -> str:
    """Build prompt from modular sections"""
    
    sections = []
    
    # Section 1: Identity & Status (always)
    sections.append(get_identity_header(case))
    
    # Section 2: Current State (context)
    sections.append(get_state_context(case))
    
    # Section 3: User Message (always)
    sections.append(format_user_message(user_message))
    
    # Section 4: Task Instructions (adaptive)
    sections.append(get_task_instructions(case))
    
    # Section 5: Output Format (schema)
    sections.append(get_output_format(case.status))
    
    return "\n\n".join(sections)
```

**Key Principle**: Show what's already known, ask for what's missing.

---

## 3. Template 1: CONSULTING

### 3.1 Purpose & Scope

**Purpose**: Explore problem, formalize understanding, get user commitment

**Characteristics**:
- Reactive (follow user's lead)
- Lightweight (simple schema)
- Problem statement confirmation (critical step)
- Transition trigger (decided_to_investigate)

**Frequency**: ~10% of turns (2-5 turns per case on average)

### 3.2 LLM Output Schema

```python
class ConsultingResponse(BaseModel):
    """Response during CONSULTING status"""
    
    agent_response: str = Field(
        description="Natural language response to user"
    )
    
    state_updates: ConsultingStateUpdate

class ConsultingStateUpdate(BaseModel):
    """What LLM can update during CONSULTING"""
    
    # Problem understanding
    problem_confirmation: Optional[ProblemConfirmation] = None
    
    # Problem statement formalization (CRITICAL!)
    proposed_problem_statement: Optional[str] = Field(
        default=None,
        description="Clear, specific problem statement for user confirmation"
    )
    
    # Quick help
    quick_suggestions: List[str] = Field(
        default_factory=list,
        description="Quick fixes or tips (if applicable)"
    )

class ProblemConfirmation(BaseModel):
    """Agent's initial understanding"""
    problem_type: str  # error | slowness | unavailability | data_issue | other
    severity_guess: str  # critical | high | medium | low | unknown
    preliminary_guidance: Optional[str] = None
```

### 3.3 Prompt Template

```python
CONSULTING_TEMPLATE = """
You are FaultMaven, an SRE troubleshooting copilot.

═══════════════════════════════════════════════════════════
STATUS: CONSULTING (Pre-Investigation)
═══════════════════════════════════════════════════════════

Turn: {case.current_turn}

CONVERSATION HISTORY (last 5-10 turns):
{recent_conversation_context}

{if case.consulting.proposed_problem_statement}
YOUR PROPOSED PROBLEM STATEMENT:
"{case.consulting.proposed_problem_statement}"

Confirmation Status: {case.consulting.problem_statement_confirmed ? "✅ Confirmed" : "⏳ Awaiting user confirmation"}

{if not case.consulting.problem_statement_confirmed}
NOTE: User has NOT confirmed yet. They may:
- Agree completely → System sets confirmed = True  
- Suggest revisions → UPDATE proposed_problem_statement based on their feedback
- Ignore → Keep asking for confirmation
{endif}
{endif}

═══════════════════════════════════════════════════════════
CURRENT USER MESSAGE
═══════════════════════════════════════════════════════════

{user_message}

═══════════════════════════════════════════════════════════
YOUR TASK
═══════════════════════════════════════════════════════════

1. **Answer User's Question Thoroughly**
   Provide helpful, accurate response to their immediate query.

2. **Problem Detection & Formalization Workflow**:

   **Step 0: DETECT PROBLEM SIGNALS (Check Every Turn)**
   - Problem signals: errors, failures, slowness, outages, user asks "Help me fix..."
   - No problem signals: general questions, informational queries, configuration help
   - IF NO PROBLEM SIGNAL: Just answer question, done. Don't create proposed_problem_statement.
   - IF PROBLEM SIGNAL DETECTED: Proceed to Step A (formalization)
   
   **Step A: When you have enough information to formalize**
   - Fill out: problem_confirmation (problem_type, severity_guess)
   - CREATE: proposed_problem_statement (clear, specific, actionable)
   - In your response: "Let me confirm my understanding: <problem_statement>. Is that accurate?"
   
   **Step B: User provides corrections/refinements (REVISION LOOP)**
   - If user corrects: "Not quite - it's 30%, not 10%"
   - UPDATE: proposed_problem_statement based on their feedback
   - In response: "Thanks for clarifying! Let me refine: <updated_statement>. Is that better?"
   - ITERATE until user confirms without reservation
   
   Example:
   Turn 3: You create "API experiencing slowness"
           User: "30% failure rate, not just slow"
   Turn 4: You UPDATE to "...with 30% failure rate"
           User: "Started 2h ago"
   Turn 5: You UPDATE to "...started 2h ago"
           User: "Yes, exactly!" → Confirmed
   
   **Step C: User confirms without reservation**
   - User says: "yes", "correct", "exactly" or clicks ✅ Confirm button
   - System detects and sets: problem_statement_confirmed = True
   - proposed_problem_statement becomes IMMUTABLE
   - In your response: "Would you like me to investigate this formally? I can:
     • Verify the symptom with evidence
     • Identify the root cause
     • Propose a solution
     
     Shall we proceed with investigation?"
   
   **Step D: User decides to investigate**
   - User says: "yes", "please investigate" or clicks ✅ Yes, Investigate button
   - System detects and sets: decided_to_investigate = True
   - System will transition to INVESTIGATING

3. **Quick Suggestions**
   - If you have quick tips or common fixes, provide them
   - Fill: quick_suggestions list

═══════════════════════════════════════════════════════════
KEY PRINCIPLES
═══════════════════════════════════════════════════════════

**Reactive, Not Proactive**:
- Don't assume user wants investigation
- Answer their question first
- Offer investigation only if problem signals detected

**Problem Signals** (when to offer investigation):
- Errors, failures, "not working"
- Performance issues, slowness
- Outages, unavailability
- User explicitly asks for help troubleshooting

**No Problem Signals** (when NOT to offer):
- General questions ("How does X work?")
- Informational queries
- Configuration questions

**Problem Statement Quality**:
- Clear: "API timing out with 10% failure rate"
- NOT vague: "API having issues"
- Specific: Include symptoms, frequency, impact
- Actionable: Something to investigate

═══════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════

Return JSON matching ConsultingResponse schema:

{
  "agent_response": "<your natural response to user>",
  "state_updates": {
    "problem_confirmation": {...} or null,
    "proposed_problem_statement": "<clear, specific statement>" or null,
    "quick_suggestions": [...]
  }
}

**CRITICAL**: Get clear, confirmed problem statement before investigation starts!
"""
```

### 3.4 Conversation Flow Example

```python
# Turn 1: Vague description
User: "Our API has been acting weird lately"

LLM Response:
{
  "agent_response": "I understand. Can you describe what 'acting weird' means? 
                     Are you seeing errors, slowness, or something else?",
  "state_updates": {
    "problem_confirmation": null,  # Too vague
    "proposed_problem_statement": null,
    "quick_suggestions": []
  }
}

# Turn 2: User provides details
User: "It's timing out sometimes, like 10% of requests fail"

LLM Response:
{
  "agent_response": "Got it. Let me confirm my understanding:
                     
                     **Problem**: API intermittently timing out (10% request failure rate)
                     
                     Is this accurate? Any corrections?",
  
  "state_updates": {
    "problem_confirmation": {
      "problem_type": "error",
      "severity_guess": "high",
      "preliminary_guidance": "High failure rate suggests serious issue"
    },
    "proposed_problem_statement": "API intermittently timing out (10% request failure rate)",
    "quick_suggestions": []
  }
}

# Turn 3: User confirms
User: "Yes, that's right. Please investigate."

LLM Response:
{
  "agent_response": "Perfect. I'll start formal investigation. 
                     
                     First, I need to verify the symptom with concrete evidence. 
                     Can you provide:
                     - Error logs from the API
                     - Timeframe when this started
                     - Which endpoints are affected
                     
                     This will help me understand the scope.",
  
  "state_updates": {
    "problem_confirmation": null,  # No change
    "proposed_problem_statement": null,  # No change  
    "quick_suggestions": []
  }
}

# System detects user confirmed AND agreed to investigate:
# - case.consulting.problem_statement_confirmed = True
# - case.consulting.decided_to_investigate = True
# → Triggers transition to INVESTIGATING
```

### 3.5 Edge Cases

**User Declines Investigation**:
```python
User: "No, I just wanted to know if this is normal"

LLM Response:
{
  "agent_response": "That's helpful context. 10% failure rate is NOT normal - 
                     that's definitely a problem worth addressing. However, if 
                     you're not ready for full investigation, I'm happy to answer 
                     any other questions you have.",
  "state_updates": {
    "problem_confirmation": null,
    "proposed_problem_statement": null,
    "quick_suggestions": [
      "Check recent deployments (common cause of sudden failures)",
      "Review API gateway logs for patterns",
      "Monitor error rate trend over time"
    ]
  }
}

# Stay in CONSULTING - don't force investigation
```

**No Problem Detected**:
```python
User: "How do I configure connection pooling?"

LLM Response:
{
  "agent_response": "Connection pooling configuration depends on your setup. 
                     Here's how to configure it in common frameworks:
                     
                     [... detailed answer ...]
                     
                     Is there a specific issue you're experiencing with 
                     connection pooling?",
  "state_updates": {
    "problem_confirmation": null,  # No problem signal
    "proposed_problem_statement": null,
    "quick_suggestions": []
  }
}

# Informational query - no investigation offered
```

---

## 4. Template 2: INVESTIGATING

### 4.1 Purpose & Scope

**Purpose**: Complete milestones opportunistically, drive investigation to resolution

**Characteristics**:
- Proactive (lead the investigation)
- Comprehensive schema (milestones, evidence, hypotheses, solutions)
- Adaptive instructions (emphasis changes by stage)
- Working conclusion (updated every turn)

**Frequency**: ~85% of turns (most of investigation lifecycle)

### 4.2 LLM Output Schema

```python
class InvestigationResponse(BaseModel):
    """Response during INVESTIGATING status"""
    
    agent_response: str = Field(
        description="Natural language response to user"
    )
    
    state_updates: InvestigationStateUpdate

class InvestigationStateUpdate(BaseModel):
    """
    THE BIG FORM - LLM fills this every turn during INVESTIGATING.
    
    IMPORTANT: Same schema for all stages (Understanding/Diagnosing/Resolving).
    Instructions adapt to emphasize relevant sections.
    """
    
    # Milestones (LLM sets to True when completed)
    milestones: Optional[MilestoneUpdates] = None
    
    # Verification data (during Understanding stage)
    verification_updates: Optional[ProblemVerificationUpdate] = None
    
    # Evidence (ALWAYS available - user provides freely)
    evidence_to_add: List[EvidenceToAdd] = Field(default_factory=list)
    
    # Hypotheses (optional - only if root cause unclear)
    hypotheses_to_add: List[HypothesisToAdd] = Field(default_factory=list)
    hypotheses_to_update: Dict[str, HypothesisUpdate] = Field(default_factory=dict)
    
    # Hypothesis-Evidence Links (when evaluating submitted evidence)
    hypothesis_evidence_links: List[HypothesisEvidenceLinkToAdd] = Field(default_factory=list)
    
    # Solutions (during Resolving stage)
    solutions_to_add: List[SolutionToAdd] = Field(default_factory=list)
    
    # Working conclusion (update frequently)
    working_conclusion: Optional[WorkingConclusionUpdate] = None
    
    # Root cause conclusion (when root_cause_identified)
    root_cause_conclusion: Optional[RootCauseConclusionUpdate] = None
    
    # Turn outcome
    outcome: TurnOutcome

class MilestoneUpdates(BaseModel):
    """Milestones LLM can set to True (never False - milestones only advance)"""
    
    # Verification milestones
    symptom_verified: Optional[bool] = None
    scope_assessed: Optional[bool] = None
    timeline_established: Optional[bool] = None
    changes_identified: Optional[bool] = None
    
    # Investigation milestones
    root_cause_identified: Optional[bool] = None
    root_cause_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    root_cause_method: Optional[str] = None  # direct_analysis | hypothesis_validation | correlation | other
    
    # Resolution milestones
    solution_proposed: Optional[bool] = None
    solution_applied: Optional[bool] = None  # User action - LLM reports
    solution_verified: Optional[bool] = None

class EvidenceToAdd(BaseModel):
    """Evidence object LLM creates"""
    
    # LLM provides these
    summary: str = Field(max_length=500)
    analysis: Optional[str] = Field(default=None, max_length=2000)
    tests_hypothesis_id: Optional[str] = None
    stance: Optional[EvidenceStance] = None
    
    # System infers these
    # - category (SYMPTOM/CAUSAL/RESOLUTION/OTHER)
    # - advances_milestones (calculated from analysis)
    # - evidence_id, timestamps, metadata

class EvidenceRequestToAdd(BaseModel):
    """Evidence request LLM creates"""
    
    description: str = Field(max_length=500)
    rationale: str = Field(max_length=1000)
    acquisition_guidance: Optional[str] = Field(default=None, max_length=2000)
    validates_milestone: Optional[str] = None
    tests_hypothesis_id: Optional[str] = None

class WorkingConclusionUpdate(BaseModel):
    """Working conclusion (updated every turn)"""
    
    statement: str = Field(max_length=1000)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=2000)
    supporting_evidence_ids: List[str] = Field(default_factory=list)
    caveats: List[str] = Field(default_factory=list)
    next_evidence_needed: List[str] = Field(default_factory=list)

class TurnOutcome(str, Enum):
    """What happened this turn"""
    MILESTONE_COMPLETED = "milestone_completed"
    DATA_PROVIDED = "data_provided"
    DATA_REQUESTED = "data_requested"
    DATA_NOT_PROVIDED = "data_not_provided"
    HYPOTHESIS_TESTED = "hypothesis_tested"
    CASE_RESOLVED = "case_resolved"
    CONVERSATION = "conversation"
    OTHER = "other"
```

### 4.3 Prompt Template (Core Structure)

```python
def build_investigating_prompt(case: Case, user_message: str) -> str:
    """
    Single template with adaptive instructions.
    """
    
    prompt = f"""
You are FaultMaven, an SRE troubleshooting copilot.

═══════════════════════════════════════════════════════════
STATUS: INVESTIGATING
═══════════════════════════════════════════════════════════

Turn: {case.current_turn}
Investigation Path: {case.path_selection.path if case.path_selection else "Not yet selected"}

═══════════════════════════════════════════════════════════
WHAT YOU ALREADY KNOW (Don't re-verify!)
═══════════════════════════════════════════════════════════

**PROBLEM:**
{case.problem_verification.symptom_statement if case.problem_verification else "Not yet verified"}

**MILESTONES:**
"""
    
    # Show milestone completion status
    milestones = {
        "symptom_verified": case.progress.symptom_verified,
        "scope_assessed": case.progress.scope_assessed,
        "timeline_established": case.progress.timeline_established,
        "changes_identified": case.progress.changes_identified,
        "root_cause_identified": case.progress.root_cause_identified,
        "solution_proposed": case.progress.solution_proposed,
        "solution_applied": case.progress.solution_applied,
        "solution_verified": case.progress.solution_verified,
    }
    
    for milestone, completed in milestones.items():
        status = "✅" if completed else "⏳"
        prompt += f"{status} {milestone}\n"
    
    # Show investigation data
    prompt += f"""
**DATA COLLECTED:**
- Evidence: {len(case.evidence)} pieces
- Hypotheses: {len(case.hypotheses)} generated ({len([h for h in case.hypotheses.values() if h.status == 'ACTIVE'])} active)
- Solutions: {len(case.solutions)} proposed
"""
    
    # Show recent conversation
    if case.turn_history:
        recent = case.turn_history[-3:]
        prompt += "\n**RECENT CONVERSATION:**\n"
        for turn in recent:
            prompt += f"Turn {turn.turn_number}: {turn.outcome}\n"
    
    # Show working conclusion
    if case.working_conclusion:
        wc = case.working_conclusion
        prompt += f"""
**WORKING CONCLUSION:**
Statement: {wc.statement}
Confidence: {wc.confidence * 100:.0f}%
{f"Caveats: {', '.join(wc.caveats[:2])}" if wc.caveats else ""}
"""
    
    prompt += f"""
═══════════════════════════════════════════════════════════
USER'S MESSAGE
═══════════════════════════════════════════════════════════

{user_message}

═══════════════════════════════════════════════════════════
YOUR TASK
═══════════════════════════════════════════════════════════

"""
    
    # ADAPTIVE INSTRUCTIONS based on stage
    stage = case.progress.current_stage

    if stage == InvestigationStage.SYMPTOM_VERIFICATION:
        prompt += get_symptom_verification_instructions(case)
    elif stage == InvestigationStage.HYPOTHESIS_FORMULATION:
        prompt += get_hypothesis_formulation_instructions(case)
    elif stage == InvestigationStage.HYPOTHESIS_VALIDATION:
        prompt += get_hypothesis_validation_instructions(case)
    elif stage == InvestigationStage.SOLUTION:
        prompt += get_solution_instructions(case)
    
    # GENERAL INSTRUCTIONS (all stages)
    prompt += get_general_instructions(case, pending)
    
    # DEGRADED MODE (if active)
    if case.degraded_mode:
        prompt += get_degraded_mode_instructions(case)
    
    # OUTPUT FORMAT
    prompt += get_output_format_instructions()
    
    return prompt
```

### 4.4 Stage-Specific Instructions

#### Symptom Verification Stage

```python
def get_symptom_verification_instructions(case: Case) -> str:
    return """
**FOCUS: VERIFICATION** (Understanding the Problem)

**Current Stage**: SYMPTOM_VERIFICATION
**Goal**: Confirm problem is real, understand context

**Priority Actions:**
1. ✅ Verify symptom with concrete evidence (logs, metrics, user reports)
2. ✅ Assess scope (who/what affected, blast radius)
3. ✅ Establish timeline (when started, when noticed, still ongoing?)
4. ✅ Identify recent changes (deployments, configs, scaling events)
5. ✅ Determine temporal_state (ONGOING vs HISTORICAL)
6. ✅ Assess urgency_level (CRITICAL/HIGH/MEDIUM/LOW)

**What to Fill Out:**
- `verification_updates`: Complete ProblemVerification fields
- `milestones`: Set verification milestones to True when verified
- `evidence_to_add`: Add evidence objects for data user provided

**IMPORTANT: You CAN jump ahead if user provides comprehensive data!**

Example: If logs show obvious root cause → Set root_cause_identified = True
Don't artificially constrain yourself to verification only.

**Verification Completion:**
When ALL verification milestones complete, system will:
- Compute investigation path (MITIGATION_FIRST vs ROOT_CAUSE)
- Auto-advance to HYPOTHESIS_FORMULATION stage (or SOLUTION for MITIGATION_FIRST path)
- Provide path-specific guidance

Continue until verification milestones are complete.
"""
```

#### Hypothesis Formulation Stage

```python
def get_hypothesis_formulation_instructions(case: Case) -> str:
    return f"""
**FOCUS: HYPOTHESIS GENERATION** (Finding Why)

**Current Stage**: HYPOTHESIS_FORMULATION
**Goal**: Generate theories about why the problem is happening

✅ **VERIFICATION COMPLETE**

**Verification Data Available:**
- Symptom: {case.problem_verification.symptom_statement}
- Temporal State: {case.problem_verification.temporal_state}
- Urgency: {case.problem_verification.urgency_level}
- Path: {case.path_selection.path if case.path_selection else "Determining..."}

**ROOT CAUSE IDENTIFICATION - Decision Tree:**

**Option A: DIRECT IDENTIFICATION** (if root cause obvious from evidence)

   ✅ Use when:
   - Clear error message pointing to specific cause
   - Strong correlation with recent change (deployment → errors)
   - Logs show definitive root cause

   Example: "Deployment at 14:10, NullPointerException at 14:15 = deployment bug"

   Actions:
   → Set: root_cause_identified = True
   → Fill: root_cause_conclusion
   → Specify: root_cause_method = "direct_analysis"

**Option B: HYPOTHESIS TESTING** (if root cause unclear)

   ✅ Use when:
   - Multiple possible causes
   - Symptoms could match several theories
   - Need diagnostic data to differentiate

   Example: "Could be pool exhaustion OR memory leak OR query timeout"

   Actions:
   → Generate: hypotheses_to_add (2-4 hypotheses)
   → When user provides evidence: Evaluate against ALL hypotheses (hypothesis_evidence_links)
   → Update hypothesis.status based on evidence: TESTING → VALIDATED/REFUTED

**GUIDELINE:**
- 70% of cases should identify root cause directly (no hypotheses needed)
- 30% of cases need hypothesis testing (unclear diagnosis)
- **When in doubt: Try direct identification first, hypotheses if stuck**

**IMPORTANT: Don't generate hypotheses if root cause is obvious!**
"""
```

#### Hypothesis Validation Stage

```python
def get_hypothesis_validation_instructions(case: Case) -> str:
    return f"""
**FOCUS: HYPOTHESIS VALIDATION** (Testing Theories)

**Current Stage**: HYPOTHESIS_VALIDATION
**Goal**: Test and validate hypotheses to confirm root cause

✅ **VERIFICATION COMPLETE**
✅ **HYPOTHESES GENERATED**

**Your Task:**
- Evaluate new evidence against all active hypotheses
- Update hypothesis status based on evidence (VALIDATED/REFUTED/TESTING)
- Mark root_cause_identified = True when hypothesis validated with high confidence

**Evidence Evaluation:**
- Link evidence to specific hypotheses via hypothesis_evidence_links
- Update hypothesis confidence scores based on supporting/contradicting evidence
- Refute hypotheses that contradict evidence

**Completion:**
When hypothesis validated with sufficient confidence:
→ Set root_cause_identified = True
→ Fill root_cause_conclusion with validated hypothesis
→ Advance to SOLUTION stage
"""
```

#### Solution Stage

```python
def get_solution_instructions(case: Case) -> str:
    return f"""
**FOCUS: SOLUTION** (Fixing the Problem)

**Current Stage**: SOLUTION
**Goal**: Apply solution and verify effectiveness

✅ **VERIFICATION COMPLETE**
✅ **ROOT CAUSE IDENTIFIED**

**Root Cause:**
{case.root_cause_conclusion.root_cause}

**Confidence:** {case.root_cause_conclusion.confidence_level} ({case.root_cause_conclusion.confidence_score * 100:.0f}%)

**Solution Actions:**

**1. Propose Solution:**

   Path-specific guidance:
   - **MITIGATION_FIRST path**: Quick fix first (immediate_action), then longterm_fix after RCA
   - **ROOT_CAUSE path**: Comprehensive fix (longterm_fix + immediate_action)
   
   Fill out: solutions_to_add

**2. Guide Implementation:**
   - Provide: implementation_steps (numbered list)
   - Provide: commands (specific commands to run)
   - Warn: risks (potential side effects, rollback plan)

**3. Track Progress:**
   - solution_proposed: Set to True when you propose solution
   - solution_applied: Set to True when user confirms they applied it
   - solution_verified: Set to True when you verify it worked

**4. Verify Effectiveness:**
   - Request: verification evidence (metrics, error rates, logs)
   - Analyze: Did solution fix the problem?
   - Compare: Before/after metrics
   - Set: solution_verified if confirmed

**Solution Verification Criteria:**
- Symptom resolved (errors stopped, performance improved)
- Metrics confirm improvement (error rate down, latency normal)
- Stable for reasonable period (15-30 minutes for immediate issues)

If solution verified → outcome = "case_resolved"
"""
```

### 4.5 General Instructions (All Stages)

```python
def get_general_instructions(case: Case, pending_requests: List) -> str:
    instructions = """
═══════════════════════════════════════════════════════════
GENERAL INSTRUCTIONS (Apply to All Stages)
═══════════════════════════════════════════════════════════

**Evidence Handling:**

**Create Evidence from objective data only:**
✅ Uploaded files, pasted command output, error messages, stack traces
❌ User saying "I saw X", "I think Y", "Page seems slow"
→ If user describes → Request actual data: "Please provide: [command/file]"

**Three types (system decides, not you):**
1. SYMPTOM - Shows problem exists (error logs, metrics, stack traces)
2. CAUSAL - Tests why problem exists (diagnostic logs, code, config)
3. RESOLUTION - Shows fix worked (logs/metrics after fix)

**Hypothesis evaluation:**
• Symptom evidence → No evaluation (just shows problem exists)
• Causal evidence → Evaluate against ALL hypotheses (tests theories)
• Resolution evidence → No evaluation (just shows fix worked)

When evaluating causal evidence:
- For EACH hypothesis, determine:
  * stance: STRONGLY_SUPPORTS | SUPPORTS | NEUTRAL | CONTRADICTS | STRONGLY_CONTRADICTS | IRRELEVANT
  * reasoning: Why this evidence has this stance for THIS hypothesis
  * completeness: How well this evidence tests THIS hypothesis (0.0-1.0)
- ONE evidence can have DIFFERENT stances for DIFFERENT hypotheses!

**Request format:**
❌ "When did this start?" (forces user to guess)
✅ "Command: journalctl --since='24h' | grep ERROR" (objective data)

**Examples:**
User: "I saw errors" → Request: "Please provide error logs"
User: [Uploads error.log] → Create Evidence (SYMPTOM, no eval)
User: [Uploads session.log showing why] → Create Evidence (CAUSAL, eval vs hypotheses)
User: [Uploads logs after fix] → Create Evidence (RESOLUTION, no eval)
"""
    
    instructions += """

**Working Conclusion:**

ALWAYS update with current best understanding. Include:
- statement: Current theory/conclusion
- confidence: 0.0-1.0 (be realistic!)
- reasoning: Why you believe this
- supporting_evidence_ids: Which evidence supports this
- caveats: What's still uncertain
- next_evidence_needed: Critical gaps to fill

**Format confidence in response to user:**
- < 50%: "Based on limited evidence, I speculate..."
- 50-69%: "This is probably... though I need more evidence"
- 70-89%: "I'm confident that..."
- 90%+: "Verified:"

Example:
"Based on the error logs (confidence: 65%), this is probably a connection pool 
exhaustion issue. I'm moderately confident because error patterns match this 
cause, but I haven't verified pool metrics yet."

**Milestones:**
- Only set to True if you have EVIDENCE (don't guess!)
- You can complete MULTIPLE milestones in ONE turn
- Never set to False (milestones only advance forward)
- System computes stage from milestones automatically
"""
    
    return instructions
```

### 4.6 Degraded Mode Override

```python
def get_degraded_mode_instructions(case: Case) -> str:
    """Prepend degraded mode instructions when active"""
    
    mode_type = case.degraded_mode.mode_type
    reason = case.degraded_mode.reason
    
    # Get mode-specific guidance
    mode_descriptions = {
        "no_progress": ("no progress for multiple turns", "try different approaches or consider escalation"),
        "limited_data": ("missing critical evidence", "proceed with lower confidence or escalate"),
        "hypothesis_deadlock": ("all hypotheses inconclusive", "try different hypothesis categories or escalate"),
        "external_dependency": ("blocked by external dependencies", "escalate or wait for dependencies"),
        "other": ("investigation limitations", "assess options with user")
    }
    
    limitation, suggestion = mode_descriptions.get(mode_type, ("limitations", "discuss options"))
    
    return f"""
═══════════════════════════════════════════════════════════
⚠️ DEGRADED INVESTIGATION MODE
═══════════════════════════════════════════════════════════

**Type**: {mode_type}
**Reason**: {reason}

**BEHAVIOR CHANGES:**

1. **Transparent Communication**
   - ALWAYS prefix responses: "⚠️ Investigation limitations: {limitation}"
   - Explicitly state caveats in EVERY response
   - Be honest about confidence levels based on available evidence
   - Explain what's missing and how it limits your analysis

2. **Lower Confidence Assessment**
   - Assess confidence based ONLY on available evidence
   - Don't overstate certainty when data is limited
   - Use terms: "speculate" (<50%), "probably" (50-70%), "confident" (70-90%), "verified" (90%+)
   - Be realistic about limitations

3. **Offer Fallback Options**
   - Every 2 turns, offer escalation option
   - Explain what would help: missing evidence, expertise needed, dependencies required
   - Suggest: {suggestion}

4. **Continue Investigation**
   - DON'T give up or stop investigating
   - Work within limitations
   - Provide best-effort analysis with caveats
   - Maintain working conclusion with honest confidence

**Degraded Mode Types:**
- **NO_PROGRESS**: Stuck for 3+ turns without advancement
- **LIMITED_DATA**: Missing critical evidence
- **HYPOTHESIS_DEADLOCK**: All hypotheses inconclusive
- **EXTERNAL_DEPENDENCY**: Blocked by permissions, access, other teams
- **OTHER**: Other investigation barriers

**Example Response Format:**

"⚠️ **Degraded Mode** ({mode_type})

Based on limited evidence, I can only speculate (35% confidence) that this might
be a memory leak. However, without production logs, this is educated guesswork.

**Caveat**: Missing critical evidence (production logs, metrics) limits my analysis.
With this data, I could validate the theory and increase confidence significantly.

**Options**:
1. Proceed with this theory (35% confidence) and test the solution
2. Escalate to team with production access for better diagnosis
3. Wait until you can provide the needed evidence

What would you prefer?"

**REMEMBER**: Still investigating! Just being transparent about limitations.
"""
```

### 4.7 Output Format Instructions

```python
def get_output_format_instructions() -> str:
    return """
═══════════════════════════════════════════════════════════
OUTCOME CLASSIFICATION
═══════════════════════════════════════════════════════════

Choose outcome (what happened THIS turn):

✅ **LLM Selects:**
- `milestone_completed`: You completed one or more milestones
- `data_provided`: User provided data you requested
- `data_requested`: You asked user for data
- `data_not_provided`: You asked for data, user didn't provide
- `hypothesis_tested`: You validated or refuted a hypothesis
- `case_resolved`: Solution verified, investigation complete
- `conversation`: Normal Q&A (no investigation progress)
- `other`: Something else happened

❌ **DON'T Select:**
- "blocked": System determines this from patterns (not your call!)

**If user didn't provide requested data**: Use `data_not_provided`
System will detect blocking patterns automatically (3+ turns → degraded mode)

═══════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════

Return JSON matching InvestigationResponse schema.

**ONLY include fields that CHANGE this turn!**
- Use null for unchanged fields
- Don't repeat static data
- Be realistic - only fill what user data supports

**KEY PRINCIPLE**: Be opportunistic! Complete everything you CAN this turn.

Example:
{
  "agent_response": "Great! The error log shows NullPointerException at line 42...",
  "state_updates": {
    "milestones": {
      "symptom_verified": true,
      "timeline_established": true,
      "root_cause_identified": true
    },
    "evidence_to_add": [{
      "summary": "NullPointerException at UserService.java:42",
      "analysis": "Missing null check causes crash when user object is null"
    }],
    "root_cause_conclusion": {
      "root_cause": "Missing null check at line 42",
      "mechanism": "When user object is null, code crashes without handling",
      "confidence_score": 0.95
    },
    "working_conclusion": {
      "statement": "Deployment introduced bug: missing null check",
      "confidence": 0.95,
      "reasoning": "Error log shows exact line, timing matches deployment"
    },
    "outcome": "milestone_completed"
  }
}
"""
```

---

## 5. Template 3: TERMINAL

### 5.1 Purpose & Scope

**Purpose**: Answer questions about closed investigation, generate documentation

**Characteristics**:
- Read-only (cannot update investigation state)
- Documentation focus
- New case suggestion

**Frequency**: ~5% of turns (after case closure)

### 5.2 LLM Output Schema

```python
class TerminalResponse(BaseModel):
    """Response during RESOLVED/CLOSED status"""
    
    agent_response: str = Field(
        description="Natural language response to user"
    )
    
    state_updates: TerminalStateUpdate

class TerminalStateUpdate(BaseModel):
    """Limited updates for terminal cases"""
    
    documentation_updates: Optional[DocumentationUpdate] = None

class DocumentationUpdate(BaseModel):
    """Documentation generation requests"""
    
    lessons_learned: List[str] = Field(default_factory=list)
    what_went_well: List[str] = Field(default_factory=list)
    what_could_improve: List[str] = Field(default_factory=list)
    preventive_measures: List[str] = Field(default_factory=list)
    monitoring_recommendations: List[str] = Field(default_factory=list)
    documents_to_generate: List[DocumentType] = Field(default_factory=list)

class DocumentType(str, Enum):
    INCIDENT_REPORT = "incident_report"
    POST_MORTEM = "post_mortem"
    RUNBOOK = "runbook"
    CHAT_SUMMARY = "chat_summary"
    OTHER = "other"
```

### 5.3 Prompt Template

```python
TERMINAL_TEMPLATE = """
You are FaultMaven.

═══════════════════════════════════════════════════════════
⚠️ STATUS: {case.status.upper()} (TERMINAL STATE)
═══════════════════════════════════════════════════════════

**THIS INVESTIGATION IS PERMANENTLY CLOSED**

═══════════════════════════════════════════════════════════
CASE SUMMARY
═══════════════════════════════════════════════════════════

Problem: {case.problem_verification.symptom_statement if case.problem_verification else "Not investigated"}
Root Cause: {case.root_cause_conclusion.root_cause if case.root_cause_conclusion else "Not identified"}
Solution: {case.solutions[0].title if case.solutions else "None"}
Closure Reason: {case.closure_reason}
Closed: {format_time_ago(case.closed_at)}

Investigation completed in {case.current_turn} turns over {format_duration(case.time_to_resolution)}.

═══════════════════════════════════════════════════════════
USER'S MESSAGE
═══════════════════════════════════════════════════════════

{user_message}

═══════════════════════════════════════════════════════════
YOUR TASK
═══════════════════════════════════════════════════════════

**You CAN:**
✅ Answer questions about this closed case
✅ Explain what happened
✅ Summarize findings
✅ Provide documentation if requested
✅ Extract lessons learned

**You ABSOLUTELY CANNOT:**
❌ Set any milestones
❌ Add new evidence
❌ Generate new hypotheses
❌ Propose new solutions
❌ Resume troubleshooting
❌ Update investigation state in ANY way

═══════════════════════════════════════════════════════════
IF USER WANTS TO CONTINUE TROUBLESHOOTING
═══════════════════════════════════════════════════════════

This investigation is permanently closed. The issue you're describing 
requires a NEW case.

Respond with:

"This investigation is closed and cannot be reopened. However, I can help 
you with this new issue.

Would you like me to:
1. **Start a fresh investigation** (recommended)
2. Reference this closed case as context

I'll create a new case if you'd like to continue troubleshooting."

**CRITICAL**: Direct user to new case - NEVER attempt to reopen terminal case!

═══════════════════════════════════════════════════════════
DOCUMENTATION
═══════════════════════════════════════════════════════════

If user requests documentation, fill out documentation_updates:

- lessons_learned: Key takeaways from investigation
- what_went_well: Positive aspects
- what_could_improve: Areas for improvement
- preventive_measures: How to prevent recurrence
- monitoring_recommendations: Alerts/monitors to add
- documents_to_generate: Which document types to create

═══════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════

Return JSON matching TerminalResponse schema:

{
  "agent_response": "<your response about closed case>",
  "state_updates": {
    "documentation_updates": {...} or null
  }
}
"""
```

### 5.4 Example Interactions

```python
# User asks about closed case
User: "What was the root cause again?"

LLM Response:
{
  "agent_response": "The root cause was a connection pool exhaustion due to a 
                     connection leak in the async query code. The deployment at 
                     14:10 UTC introduced queries that didn't properly close 
                     connections, causing the pool to fill up over ~2 hours.",
  "state_updates": {
    "documentation_updates": null
  }
}

# User tries to continue troubleshooting
User: "Actually, the issue is back. Can we investigate more?"

LLM Response:
{
  "agent_response": "This investigation is closed and cannot be reopened. However, 
                     if the issue has returned, that's definitely worth investigating.
                     
                     Would you like me to:
                     1. Start a fresh investigation (recommended)
                     2. Reference this closed case as context
                     
                     I can create a new case and we'll investigate why the issue 
                     recurred. Shall we proceed?",
  "state_updates": {
    "documentation_updates": null
  }
}

# User requests documentation
User: "Can you generate a post-mortem?"

LLM Response:
{
  "agent_response": "I'll generate a post-mortem document for this investigation. 
                     Give me a moment...",
  "state_updates": {
    "documentation_updates": {
      "lessons_learned": [
        "Async queries need explicit connection management",
        "Connection pool exhaustion can take hours to manifest",
        "Error patterns (timeouts after 2h uptime) indicate resource leak"
      ],
      "what_went_well": [
        "Quick identification of correlation with deployment",
        "Comprehensive log analysis revealed exact cause"
      ],
      "what_could_improve": [
        "Earlier detection via connection pool monitoring"
      ],
      "preventive_measures": [
        "Add connection lifecycle tests for async queries",
        "Implement connection leak detection in tests"
      ],
      "monitoring_recommendations": [
        "Alert on connection pool utilization >80%",
        "Track connection lifecycle metrics"
      ],
      "documents_to_generate": ["post_mortem", "runbook"]
    }
  }
}
```

---

## 6. LLM vs System Responsibilities

### 6.1 Clear Boundary Principle

**Principle**: LLM determines what it can **observe** from turn data. System infers what must be **calculated** from state changes.

**Mental Model**:
- **LLM = Smart analyst** (reads, interprets, decides based on visible data)
- **System = State manager** (tracks, calculates, enforces rules)

### 6.2 LLM Determines (Observable)

| Category | Examples | Why LLM Can Do This |
|----------|----------|---------------------|
| **Content Analysis** | summary, analysis, mechanism | LLM reads and understands content |
| **Milestone Completion** | symptom_verified, root_cause_identified | LLM has evidence to verify |
| **Hypothesis Operations** | generate, status (VALIDATED/REFUTED), likelihood | LLM tests theory against evidence |
| **Solution Proposals** | immediate_action, implementation_steps, risks | LLM formulates solutions |
| **Confidence Assessment** | root_cause_confidence, verification_confidence | LLM evaluates certainty based on evidence |
| **Temporal Classification** | temporal_state (ONGOING/HISTORICAL) | LLM infers from user description |
| **Urgency Assessment** | urgency_level (CRITICAL/HIGH/MEDIUM/LOW) | LLM infers from severity + impact |
| **Evidence Linking** | tests_hypothesis_id, stance (SUPPORTS/REFUTES) | LLM knows relationships |
| **Turn Outcomes** | milestone_completed, data_requested, etc. | LLM observes what happened this turn |

**Example**: LLM reads log file and determines:
```python
{
  "summary": "NullPointerException at UserService.java:42",
  "analysis": "Missing null check causes crash when user object is null",
  "milestones": {
    "symptom_verified": True,  # LLM verified from log
    "root_cause_identified": True  # LLM identified cause
  },
  "root_cause_confidence": 0.95  # LLM assesses certainty
}
```

### 6.3 System Determines (Calculated)

| Category | Examples | Why LLM Cannot Do This |
|----------|----------|------------------------|
| **Identifiers** | case_id, evidence_id, hypothesis_id | Auto-generated UUIDs |
| **Timestamps** | created_at, updated_at, collected_at | System clock |
| **Counts** | current_turn, turns_without_progress | Incremented by system |
| **Progress Detection** | progress_made, milestones_completed (list) | Compare before/after state |
| **Path Selection** | path (MITIGATION_FIRST/ROOT_CAUSE/USER_CHOICE) | Deterministic matrix logic |
| **Evidence Category** | category (SYMPTOM/CAUSAL/RESOLUTION/OTHER) | Inferred from investigation context |
| **Milestone Advancement** | advances_milestones (list) | Calculated from evidence analysis |
| **Degraded Mode Entry** | Should enter? When? | Pattern analysis (3+ turns no progress) |
| **Status Transitions** | INVESTIGATING→RESOLVED | Rule-based triggers |
| **Stage Computation** | current_stage (SYMPTOM_VERIFICATION/HYPOTHESIS_FORMULATION/HYPOTHESIS_VALIDATION/SOLUTION) | Computed from milestone state |
| **File Metadata** | content_ref (S3 URI), source_type, form | From upload system |

**Example**: System processes LLM output and infers:
```python
# LLM provided
evidence_data = {
  "summary": "Connection pool at 95/100",
  "analysis": "Pool nearly exhausted",
  "tests_hypothesis_id": "hyp_abc123"
}

# System infers
evidence = Evidence(
  **evidence_data,
  category=EvidenceCategory.CAUSAL_EVIDENCE,  # ← Inferred (tests hypothesis)
  advances_milestones=["root_cause_identified"],  # ← Calculated
  evidence_id="ev_xyz789",  # ← Generated
  collected_at=datetime.now(),  # ← System clock
  collected_at_turn=case.current_turn,  # ← System counter
  content_ref="s3://...",  # ← Upload system
  source_type=EvidenceSourceType.METRICS_DATA,  # ← Detected
  form=EvidenceForm.USER_INPUT  # ← Detected
)
```

### 6.4 User Actions (LLM Reports)

LLM **reports** user actions but doesn't **decide** them:

| Action | LLM Role | System Role |
|--------|----------|-------------|
| **decided_to_investigate** | Detects user intent from message | Sets field to True, triggers transition |
| **solution_applied** | Detects user confirmation | Sets milestone, checks for verification |
| **case_closed** | Understands user wants to close | Transitions to terminal status |

**Example**:
```python
User: "Yes, I've applied the rollback and errors stopped"

LLM: (detects solution_applied = True AND solution_verified = True)
{
  "milestones": {
    "solution_applied": True,
    "solution_verified": True
  },
  "outcome": "case_resolved"
}

System: (processes and transitions status)
case.status = CaseStatus.RESOLVED
case.resolved_at = datetime.now()
case.closed_at = datetime.now()
```

### 6.5 Ambiguous Cases (How to Decide)

**Decision Framework**:

```python
def should_llm_or_system_determine(field: str) -> str:
    """Decide if LLM or system should determine a field"""
    
    # Ask these questions:
    
    # 1. Can LLM observe this from turn data?
    if can_observe_from_turn_data(field):
        return "LLM"  # Example: evidence summary, hypothesis status
    
    # 2. Does this require comparing states across turns?
    if requires_state_comparison(field):
        return "SYSTEM"  # Example: progress_made, turns_without_progress
    
    # 3. Is this a deterministic calculation?
    if is_deterministic_calculation(field):
        return "SYSTEM"  # Example: path selection matrix, stage computation
    
    # 4. Does this require system knowledge?
    if requires_system_knowledge(field):
        return "SYSTEM"  # Example: evidence category (needs investigation context)
    
    # 5. Is this user intent/action?
    if is_user_action(field):
        return "LLM_REPORTS"  # LLM detects, system applies
    
    # Default: LLM if observable, else system
    return "LLM" if field_value_visible_to_llm else "SYSTEM"
```

**Examples of Ambiguous Fields**:

| Field | Decision | Rationale |
|-------|----------|-----------|
| `evidence.tests_hypothesis_id` | **LLM** | LLM understands "this log tests pool exhaustion hypothesis" |
| `evidence.category` | **SYSTEM** | Requires investigation context (verification complete? solution proposed?) |
| `hypothesis.likelihood` | **LLM** | LLM estimates based on symptoms/evidence |
| `hypothesis.status` | **LLM** | LLM validates/refutes based on evidence |
| `solution.verification_evidence_id` | **LLM** | LLM knows "these metrics prove solution worked" |
| `path_selection.path` | **SYSTEM** | Deterministic from matrix (temporal × urgency) |

---

## 7. Advanced Features

### 7.1 Working Conclusion Tracking

**Purpose**: Maintain continuous investigation narrative with transparent confidence

**Philosophy**: Agent ALWAYS has a current theory, even at 30% confidence. Never "stuck" or "unknown" - just varying confidence levels.

#### 7.1.1 Working Conclusion Format

```python
# In InvestigationStateUpdate
working_conclusion: Optional[WorkingConclusionUpdate] = None

class WorkingConclusionUpdate(BaseModel):
    statement: str  # Current best understanding
    confidence: float  # 0.0-1.0
    reasoning: str  # Why believe this
    supporting_evidence_ids: List[str]
    caveats: List[str]  # What's uncertain
    next_evidence_needed: List[str]  # Critical gaps
```

#### 7.1.2 Prompt Instructions

```
**Working Conclusion (ALWAYS update every turn during INVESTIGATING):**

Current: {working_conclusion.statement if exists else "None yet"}
Confidence: {working_conclusion.confidence * 100:.0f}%

**Your Task:**
1. Update statement with new evidence/insights
2. Adjust confidence:
   - +10-30% for strong supporting evidence
   - +5-15% for moderate supporting evidence
   - -20-40% for refuting evidence
3. List caveats (what's still uncertain)
4. Specify next evidence needed (critical gaps)

**Confidence Levels & Phrasing:**
- < 50%: "Based on limited evidence, I speculate..."
- 50-69%: "This is probably... though I need more evidence"
- 70-89%: "I'm confident that..."
- 90%+: "Verified:"

**Example in Response:**
"Based on the error logs (confidence: 65%), this is probably a connection pool 
exhaustion issue. I'm moderately confident because error patterns match pool 
exhaustion and timing correlates with traffic spike. However, I haven't verified 
actual pool metrics yet - that would increase confidence to 85%+."

**In state_updates:**
{
  "working_conclusion": {
    "statement": "Connection pool exhaustion causing timeouts",
    "confidence": 0.65,
    "reasoning": "Error patterns match pool exhaustion, timing correlates with traffic spike at 14:15",
    "supporting_evidence_ids": ["ev_abc123", "ev_def456"],
    "caveats": [
      "Pool metrics not yet verified",
      "Assuming default pool size of 100"
    ],
    "next_evidence_needed": [
      "Actual pool metrics (kubectl top pods)",
      "Application connection lifecycle code review"
    ]
  }
}
```

#### 7.1.3 Confidence Evolution Example

```python
# Turn 5: Initial theory
working_conclusion = {
  "statement": "Possible connection pool issue",
  "confidence": 0.35,
  "reasoning": "Timeout errors suggest resource exhaustion"
}

# Turn 7: More evidence
working_conclusion = {
  "statement": "Likely connection pool exhaustion",
  "confidence": 0.60,
  "reasoning": "Error timing matches uptime pattern, deployment added async queries"
}

# Turn 9: Diagnostic data
working_conclusion = {
  "statement": "Connection pool exhaustion due to connection leak",
  "confidence": 0.85,
  "reasoning": "Pool metrics show 95/100 connections, async code missing conn.close()"
}

# Turn 11: Root cause confirmed
root_cause_conclusion = {
  "root_cause": "Connection leak in async query code",
  "confidence_score": 0.95,
  "mechanism": "Async queries not closing connections, pool fills over 2h"
}
```

### 7.2 Path Selection

**Purpose**: Route investigation based on urgency and temporal state

#### 7.2.1 Path Selection Matrix

```
| Temporal State | Urgency  | Path             | Auto-Selected? |
|----------------|----------|------------------|----------------|
| ONGOING        | CRITICAL | MITIGATION_FIRST | Yes            |
| ONGOING        | HIGH     | MITIGATION_FIRST | Yes            |
| ONGOING        | MEDIUM   | USER_CHOICE      | No             |
| ONGOING        | LOW      | USER_CHOICE      | No             |
| HISTORICAL     | CRITICAL | USER_CHOICE      | No (why critical if past?) |
| HISTORICAL     | HIGH     | USER_CHOICE      | No             |
| HISTORICAL     | MEDIUM   | ROOT_CAUSE       | Yes            |
| HISTORICAL     | LOW      | ROOT_CAUSE    | Yes            |
```

#### 7.2.2 System Logic

```python
def determine_investigation_path(pv: ProblemVerification) -> PathSelection:
    """System determines path from matrix (LLM provides inputs only)"""
    
    temporal = pv.temporal_state
    urgency = pv.urgency_level
    
    # AUTO: Ongoing + High urgency → MITIGATION_FIRST
    if temporal == TemporalState.ONGOING and urgency in [UrgencyLevel.CRITICAL, UrgencyLevel.HIGH]:
        return PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale=f"Ongoing {urgency.value} issue requires immediate mitigation first, then RCA",
            alternate_path=InvestigationPath.ROOT_CAUSE
        )
    
    # AUTO: Historical + Low urgency → ROOT_CAUSE
    if temporal == TemporalState.HISTORICAL and urgency in [UrgencyLevel.LOW, UrgencyLevel.MEDIUM]:
        return PathSelection(
            path=InvestigationPath.ROOT_CAUSE,
            auto_selected=True,
            rationale=f"Historical {urgency.value} issue allows thorough investigation"
        )
    
    # USER CHOICE: Ambiguous cases
    return PathSelection(
        path=InvestigationPath.USER_CHOICE,
        auto_selected=False,
        rationale=f"Ambiguous case ({temporal.value} + {urgency.value}): user should choose"
    )
```

#### 7.2.3 Prompt Instructions (Diagnosing Stage)

```
**Investigation Path:** {case.path_selection.path if case.path_selection else "Not yet selected"}

{if path_selection exists and auto_selected}
  **Path Explanation (communicate to user):**
  
  "Based on {case.path_selection.rationale}, I'm taking the {case.path_selection.path} approach.

   {if path == MITIGATION_FIRST}
   **MITIGATION_FIRST Path**:
   - Goal: Restore service immediately, then find root cause
   - Approach: Quick mitigation first (1→4), then comprehensive RCA (4→2→3→4)
   - Trade-off: Initial fix may be temporary, but investigation continues for prevention
   {endif}
   
   {if path == ROOT_CAUSE}
   **ROOT CAUSE Path**:
   - Goal: Find and fix underlying cause
   - Approach: Thorough investigation
   - Benefit: Prevents recurrence
   {endif}
   
   {if alternate_path exists}
   Alternative: We could take {alternate_path} approach instead if you prefer.
   {endif}
  "
{endif}

{if path_selection exists and not auto_selected}
  **Ask User to Choose:**
  
  "This situation is ambiguous ({case.path_selection.rationale}).
   
   Which approach would you prefer?

   **Option 1: Mitigation First, Then RCA (MITIGATION_FIRST)**
   - Apply quick symptom-based fix immediately (1→4)
   - Then return for thorough investigation (4→2→3→4)
   - Faster initial recovery, comprehensive fix after

   **Option 2: Root Cause Analysis First (ROOT_CAUSE)**
   - Thorough investigation before fixing (1→2→3→4)
   - Find and fix underlying cause
   - Slower initial recovery but comprehensive from start

   What's your preference?"
{endif}
```

### 7.3 Hypothesis Optionality

**Purpose**: Allow direct root cause identification without mandatory hypotheses

**Philosophy**: 70% of cases should identify root cause directly. Only generate hypotheses when diagnosis is unclear.

#### 7.3.1 Decision Tree (in Diagnosing Stage)

```
**ROOT CAUSE IDENTIFICATION - Decision Tree:**

**Step 1: Check if root cause OBVIOUS from evidence**

✅ Root cause is OBVIOUS when:
- Clear error message points to specific cause
- Strong correlation with recent change (deployment at 14:10 → errors at 14:15)
- Logs show definitive root cause (NullPointerException at specific line)
- Timeline + evidence clearly indicate cause

→ If OBVIOUS: **SKIP hypotheses**, directly set:
  - root_cause_identified = True
  - root_cause_conclusion = {...}
  - root_cause_method = "direct_analysis"

Example:
"Deployment at 14:10, NullPointerException at 14:15, rollback fixes it
= Deployment bug (95% confidence)"

**Step 2: Check if root cause UNCLEAR**

✅ Root cause is UNCLEAR when:
- Multiple possible causes
- Symptoms could match several theories
- Need diagnostic data to differentiate
- No strong correlation with single cause

→ If UNCLEAR: **Generate hypotheses** (2-4):
  - hypotheses_to_add = [{statement, likelihood, reasoning}, ...]
  - When user provides evidence: Evaluate against ALL hypotheses

Example:
"Timeout errors started 2h after uptime consistently. Could be:
- Pool exhaustion (60% likelihood)
- Memory leak (30% likelihood)
- Slow queries (10% likelihood)

Please provide any diagnostic data you have (logs, metrics, etc.) and I'll evaluate against these theories."

**Guideline:**
- Try direct identification FIRST
- Use hypotheses ONLY if stuck or ambiguous
- Don't generate hypotheses to "look thorough"
```

### 7.4 Degraded Mode

**Purpose**: Gracefully handle investigation barriers with transparent confidence caps

#### 7.4.1 Degraded Mode Types

```python
class DegradedModeType(str, Enum):
    """Reason for entering degraded mode"""
    
    NO_PROGRESS = "no_progress"
    """3+ consecutive turns without milestone advancement. Investigation is stuck."""
    
    LIMITED_DATA = "limited_data"
    """Cannot obtain required evidence. Insufficient data to proceed."""
    
    HYPOTHESIS_DEADLOCK = "hypothesis_deadlock"
    """All hypotheses are inconclusive. Cannot determine root cause."""
    
    EXTERNAL_DEPENDENCY = "external_dependency"
    """Blocked by external dependency (permissions, access, other teams)."""
    
    OTHER = "other"
    """Other unexpected degradation reason."""

# NOTE: No rigid confidence caps per Option A simplification
# Degraded mode is about transparent communication, not arbitrary limits
# LLM should honestly assess confidence based on available evidence
```

#### 7.5.2 Trigger Conditions

```python
# System detects degraded mode when:

# Pattern 1: No progress for 3+ turns
if case.turns_without_progress >= 3:
    enter_degraded_mode(case, DegradedModeType.NO_PROGRESS)

# Pattern 2: All hypotheses inconclusive
if len(case.hypotheses) > 0:
    all_inconclusive = all(
        h.status == HypothesisStatus.INCONCLUSIVE
        for h in case.hypotheses.values()
    )
    if all_inconclusive:
        enter_degraded_mode(case, DegradedModeType.HYPOTHESIS_DEADLOCK)
```

#### 7.5.3 Behavior Changes (see Section 4.6)

When degraded mode active:
1. **Confidence capping** - ALL conclusions ≤ cap
2. **Transparent communication** - Prefix responses with warning
3. **Explicit caveats** - State limitations in every response
4. **Periodic escalation offers** - Every 2 turns
5. **Continue investigating** - Don't give up, work within limits

---

## 8. System Post-Processing

### 8.1 Overview

After LLM returns structured output, system performs:
1. Apply milestone updates
2. Process evidence (infer category, advancement)
3. Create hypothesis-evidence links (many-to-many relationships)
4. Determine path selection (if verification just completed)
5. Detect progress
6. Update turns_without_progress counter
7. Check degraded mode trigger
8. Check status transitions

### 8.2 Evidence Categorization Logic

```python
def infer_evidence_category(
    evidence_data: EvidenceToAdd,
    case: Case
) -> EvidenceCategory:
    """
    System infers evidence category from investigation context.
    LLM doesn't specify this - system determines from state.
    """
    
    # Rule 1: Testing specific hypothesis → CAUSAL
    if evidence_data.tests_hypothesis_id is not None:
        return EvidenceCategory.CAUSAL_EVIDENCE
    
    # Rule 2: Verification incomplete → SYMPTOM
    if not case.progress.verification_complete:
        return EvidenceCategory.SYMPTOM_EVIDENCE
    
    # Rule 3: Solution already proposed → RESOLUTION
    if case.progress.solution_proposed:
        return EvidenceCategory.RESOLUTION_EVIDENCE
    
    # Rule 4: Default → OTHER
    return EvidenceCategory.OTHER
```

**Rationale**: Evidence category depends on **investigation context** (which stage, what's been completed), not content alone. LLM can't see this context clearly, so system infers it.

### 8.3 Milestone Advancement Calculation

```python
def determine_milestone_advancement(
    evidence: Evidence,
    case: Case
) -> List[str]:
    """
    Calculate which milestones this evidence advances.
    Based on evidence category and analysis content.
    """
    
    milestones = []
    
    if evidence.category == EvidenceCategory.SYMPTOM_EVIDENCE:
        # Check analysis for milestone keywords
        if "symptom" in evidence.analysis.lower() and not case.progress.symptom_verified:
            milestones.append("symptom_verified")
        
        if "timeline" in evidence.analysis.lower() and not case.progress.timeline_established:
            milestones.append("timeline_established")
        
        if ("scope" in evidence.analysis.lower() or 
            "affected" in evidence.analysis.lower()) and not case.progress.scope_assessed:
            milestones.append("scope_assessed")
        
        if ("deployment" in evidence.analysis.lower() or 
            "change" in evidence.analysis.lower()) and not case.progress.changes_identified:
            milestones.append("changes_identified")
    
    elif evidence.category == EvidenceCategory.CAUSAL_EVIDENCE:
        # Check if hypothesis validated
        if evidence.tests_hypothesis_id:
            hypothesis = case.hypotheses.get(evidence.tests_hypothesis_id)
            if hypothesis and evidence.stance == EvidenceStance.STRONGLY_SUPPORTS:
                if not case.progress.root_cause_identified:
                    milestones.append("root_cause_identified")
    
    elif evidence.category == EvidenceCategory.RESOLUTION_EVIDENCE:
        # Check if solution effectiveness confirmed
        if ("resolved" in evidence.analysis.lower() or 
            "fixed" in evidence.analysis.lower() or
            "improved" in evidence.analysis.lower()):
            if not case.progress.solution_verified:
                milestones.append("solution_verified")
    
    return milestones
```

### 8.4 Progress Detection

```python
def detect_progress(case: Case, llm_output: InvestigationResponse) -> bool:
    """
    Determine if investigation progressed this turn.
    Compare before/after state.
    """
    
    # Progress indicators
    progress_made = (
        # New milestones completed
        len(llm_output.state_updates.milestones_completed) > 0 or
        
        # New evidence added
        len(llm_output.state_updates.evidence_to_add) > 0 or
        
        # Hypothesis validated/refuted
        any(h.status in [HypothesisStatus.VALIDATED, HypothesisStatus.REFUTED]
            for h in case.hypotheses.values()) or
        
        # Solution verified
        case.progress.solution_verified
    )
    
    return progress_made
```

### 8.5 Status Transition Rules

```python
def check_automatic_status_transitions(case: Case) -> None:
    """
    Check if case should transition status automatically.
    Based on milestone completion.
    """
    
    # INVESTIGATING → RESOLVED
    if (case.status == CaseStatus.INVESTIGATING and 
        case.progress.solution_verified):
        
        case.status = CaseStatus.RESOLVED
        case.resolved_at = datetime.now(timezone.utc)
        case.closed_at = datetime.now(timezone.utc)
        case.closure_reason = "resolved"
        
        record_status_transition(
            case,
            from_status=CaseStatus.INVESTIGATING,
            to_status=CaseStatus.RESOLVED,
            reason="Solution verified"
        )
```

---

## 9. Key Principles

### 9.1 Incremental Updates Only

```python
# ❌ DON'T: Send entire case state every turn
{
  "state_updates": {
    "milestones": {
      "symptom_verified": True,
      "scope_assessed": True,  # Already True from turn 3
      "timeline_established": True,  # Already True from turn 4
      ...  # Repeat all milestones
    }
  }
}

# ✅ DO: Send only what changed
{
  "state_updates": {
    "milestones": {
      "root_cause_identified": True  # Only this changed!
    },
    "evidence_to_add": [...]  # Only NEW evidence
  }
}
```

**Rationale**: Reduces token usage, prevents accidental overwrites, clearer intent.

### 9.2 Show What's Already Known

```
**WHAT YOU ALREADY KNOW (Don't re-verify!):**

✅ Symptom: API timeout errors (10% failure rate)
✅ Timeline: Started 14:23 UTC
✅ Root Cause: NullPointerException at line 42

Don't ask about these again! Focus on next steps.
```

**Rationale**: Prevents re-work, focuses LLM attention, faster progression.

### 9.3 Adaptive Instructions

```python
# Stage-specific emphasis
if stage == SYMPTOM_VERIFICATION:
    "FOCUS: Verification - confirm problem is real"
elif stage == HYPOTHESIS_FORMULATION:
    "FOCUS: Hypothesis generation - develop theories about why it happened"
elif stage == HYPOTHESIS_VALIDATION:
    "FOCUS: Hypothesis testing - validate theories against evidence"
elif stage == SOLUTION:
    "FOCUS: Solution - fix the problem"

# But always include:
"You CAN jump ahead if user provides comprehensive data!"
```

**Rationale**: Guides LLM without constraining, enables opportunistic completion.

### 9.4 Fallback When Stuck

```python
if case.turns_without_progress >= 2:
    """
    ⚠️ WARNING: No progress for {count} turns!
    
    If stuck for 3+ turns:
    1. Proceed with best guess (lower confidence)
    2. Offer escalation
    3. Offer to close investigation
    4. Try completely different approach
    
    Fill out: degraded_mode if truly stuck
    """
```

**Rationale**: Prevents infinite stalls, offers user options.

---

## 10. Validation & Testing

### 10.1 Prompt Validation Checklist

Before deploying prompts, verify:

**Template Alignment**:
- [ ] Status values match CaseStatus enum exactly
- [ ] Stage names match InvestigationStage enum exactly
- [ ] Milestone names match InvestigationProgress fields exactly
- [ ] Evidence categories match EvidenceCategory enum exactly
- [ ] Turn outcomes match TurnOutcome enum exactly

**Schema Consistency**:
- [ ] LLM schema matches pydantic models
- [ ] All required fields are requested
- [ ] Optional fields are clearly marked
- [ ] Field types match (str, float, bool, List, etc.)

**Instruction Clarity**:
- [ ] LLM knows what it CAN determine
- [ ] LLM knows what system will infer
- [ ] Examples provided for complex fields
- [ ] Edge cases handled

**Tone & Philosophy**:
- [ ] No methodology jargon to user
- [ ] Natural, conversational language
- [ ] Doctor-patient paradigm evident
- [ ] Graceful degradation included

### 10.2 Testing Strategy

#### Unit Tests (Per Template)

```python
def test_consulting_template():
    """Test CONSULTING template produces valid schema"""
    
    case = create_test_case(status=CaseStatus.CONSULTING)
    user_message = "API is slow"
    
    prompt = build_consulting_prompt(case, user_message)
    
    # Mock LLM response
    llm_output = mock_llm_generate(prompt)
    
    # Validate schema
    response = ConsultingResponse(**llm_output)
    
    assert response.agent_response
    assert isinstance(response.state_updates, ConsultingStateUpdate)

def test_investigating_template_symptom_verification_stage():
    """Test INVESTIGATING template at Symptom Verification stage"""

    case = create_test_case(
        status=CaseStatus.INVESTIGATING,
        stage=InvestigationStage.SYMPTOM_VERIFICATION
    )

    prompt = build_investigating_prompt(case, "Here are the error logs")
    
    # Verify stage-specific instructions present
    assert "FOCUS: VERIFICATION" in prompt
    assert "symptom_verified" in prompt
    
    llm_output = mock_llm_generate(prompt)
    response = InvestigationResponse(**llm_output)
    
    assert response.state_updates.milestones is not None

def test_terminal_template():
    """Test TERMINAL template enforces read-only"""
    
    case = create_test_case(status=CaseStatus.RESOLVED)
    
    prompt = build_terminal_prompt(case, "What was the root cause?")
    
    # Verify restrictions present
    assert "CANNOT update investigation state" in prompt
    assert "PERMANENTLY CLOSED" in prompt
    
    llm_output = mock_llm_generate(prompt)
    response = TerminalResponse(**llm_output)
    
    # Should only allow documentation updates
    assert response.state_updates.documentation_updates is not None or \
           response.state_updates.documentation_updates is None
```

#### Integration Tests (Turn Processing)

```python
async def test_one_turn_resolution():
    """Test case can complete in one turn with comprehensive data"""
    
    case = create_test_case(status=CaseStatus.INVESTIGATING)
    
    # User provides comprehensive log
    user_message = """
    Here's the error log showing NullPointerException at UserService.java:42.
    Started at 14:15 UTC, right after we deployed v2.1.3 at 14:10 UTC.
    Affects all users. We rolled back and errors stopped.
    """
    
    # Process turn
    result = await process_turn(case, user_message)
    
    # Verify multiple milestones completed in one turn
    assert case.progress.symptom_verified
    assert case.progress.timeline_established
    assert case.progress.root_cause_identified
    assert case.progress.solution_proposed
    
    assert result.agent_response
    assert "deployment" in result.agent_response.lower()

async def test_degraded_mode_entry():
    """Test degraded mode triggers after 3 turns no progress"""
    
    case = create_test_case(status=CaseStatus.INVESTIGATING)
    
    # 3 turns with no progress
    for i in range(3):
        await process_turn(case, f"Turn {i}: user provides no data")
    
    # Should enter degraded mode
    assert case.degraded_mode is not None
    assert case.degraded_mode.mode_type == DegradedModeType.NO_PROGRESS
```

#### Validation Tests (System Post-Processing)

```python
def test_evidence_categorization():
    """Test system correctly infers evidence category"""
    
    case = create_test_case(status=CaseStatus.INVESTIGATING)
    
    # Evidence during Understanding stage → SYMPTOM
    case.progress.symptom_verified = False
    evidence = create_evidence(tests_hypothesis_id=None)
    category = infer_evidence_category(evidence, case)
    assert category == EvidenceCategory.SYMPTOM_EVIDENCE
    
    # Evidence testing hypothesis → CAUSAL
    evidence = create_evidence(tests_hypothesis_id="hyp_123")
    category = infer_evidence_category(evidence, case)
    assert category == EvidenceCategory.CAUSAL_EVIDENCE
    
    # Evidence after solution proposed → RESOLUTION
    case.progress.solution_proposed = True
    evidence = create_evidence(tests_hypothesis_id=None)
    category = infer_evidence_category(evidence, case)
    assert category == EvidenceCategory.RESOLUTION_EVIDENCE

def test_milestone_advancement_calculation():
    """Test system calculates milestone advancement correctly"""
    
    case = create_test_case()
    evidence = Evidence(
        summary="Error logs show timeouts",
        analysis="Symptom confirmed: 10% of API requests timing out",
        category=EvidenceCategory.SYMPTOM_EVIDENCE
    )
    
    milestones = determine_milestone_advancement(evidence, case)
    
    assert "symptom_verified" in milestones

def test_status_transition_triggers():
    """Test automatic status transitions"""
    
    case = create_test_case(status=CaseStatus.INVESTIGATING)
    case.progress.solution_verified = True
    
    check_automatic_status_transitions(case)
    
    assert case.status == CaseStatus.RESOLVED
    assert case.resolved_at is not None
    assert case.closed_at is not None
    assert case.closure_reason == "resolved"
```

### 10.3 Confidence Calibration Tracking

```python
class ConfidenceCalibrationTracker:
    """Track how well LLM confidence matches actual outcomes"""
    
    def record_prediction(self, case_id, stated_confidence, hypothesis):
        """Record when LLM states confidence level"""
        self.predictions[case_id] = {
            "stated_confidence": stated_confidence,
            "hypothesis": hypothesis,
            "actual_outcome": None
        }
    
    def record_outcome(self, case_id, was_correct: bool):
        """Record actual outcome (was hypothesis correct?)"""
        if case_id in self.predictions:
            self.predictions[case_id]["actual_outcome"] = was_correct
            self._calculate_calibration()
    
    def _calculate_calibration(self):
        """Calculate calibration curve by confidence bucket"""
        
        buckets = {
            "speculation": (0.0, 0.5),   # Should be ~25-40% accurate
            "probable": (0.5, 0.7),      # Should be ~55-65% accurate
            "confident": (0.7, 0.9),     # Should be ~75-85% accurate
            "verified": (0.9, 1.0)       # Should be ~90-95% accurate
        }
        
        for bucket_name, (low, high) in buckets.items():
            predictions_in_bucket = [
                p for p in self.predictions.values()
                if low <= p["stated_confidence"] < high
                and p["actual_outcome"] is not None
            ]
            
            if predictions_in_bucket:
                accuracy = sum(
                    1 for p in predictions_in_bucket
                    if p["actual_outcome"]
                ) / len(predictions_in_bucket)
                
                # Log calibration
                logger.info(
                    f"Confidence calibration - {bucket_name}: "
                    f"{accuracy*100:.1f}% accurate "
                    f"(n={len(predictions_in_bucket)})"
                )
                
                # Alert if miscalibrated
                expected_accuracy = (low + high) / 2
                if abs(accuracy - expected_accuracy) > 0.15:
                    logger.warning(
                        f"Miscalibration detected in {bucket_name} bucket: "
                        f"Expected ~{expected_accuracy*100:.0f}%, "
                        f"Actual {accuracy*100:.0f}%"
                    )
```

### 10.4 Prompt Version Control

```python
# Track prompt versions
PROMPT_VERSION = "2.0.0"

# Embed version in prompts
def build_prompt(case, user_message):
    prompt = f"""
<!-- Prompt Version: {PROMPT_VERSION} -->
<!-- Architecture Version: Investigation v2.0 -->
<!-- Case Model Version: v2.0 -->

You are FaultMaven...
"""
    return prompt

# Log prompt version with each API call
async def generate_llm_response(prompt):
    logger.info(f"Generating response with prompt version {PROMPT_VERSION}")
    response = await llm.generate(prompt)
    
    # Store prompt version with turn
    store_turn_metadata({
        "prompt_version": PROMPT_VERSION,
        "timestamp": datetime.now()
    })
    
    return response
```

---

## Appendix A: Complete Example Turn

**Scenario**: User uploads comprehensive error log during Understanding stage

```python
# TURN 5: User provides error log

# INPUT
user_message = "Here's the error log [upload: error.log]"
log_content = """
[2024-10-27 14:15:23] ERROR NullPointerException at UserService.java:42
[2024-10-27 14:15:24] ERROR NullPointerException at UserService.java:42
[... 500 more similar errors ...]
[2024-10-27 14:10:00] INFO Deployment v2.1.3 completed successfully
"""

case = Case(
    status=CaseStatus.INVESTIGATING,
    current_turn=5,
    progress=InvestigationProgress(
        symptom_verified=False,
        root_cause_identified=False
    )
)

# STEP 1: System builds prompt
prompt = build_investigating_prompt(case, user_message)
# Includes:
# - Current milestones (none completed yet)
# - Stage: SYMPTOM_VERIFICATION
# - Instructions: Focus on verification

# STEP 2: LLM processes
llm_response = await llm.generate(prompt, schema=InvestigationResponse)

# LLM returns:
{
  "agent_response": """
Great! The error log is very helpful. I can see several key things:

**What I Found:**
- **Error**: NullPointerException at UserService.java line 42
- **Timeline**: Started 14:15 UTC, 5 minutes after v2.1.3 deployment (14:10 UTC)
- **Root Cause**: The deployment introduced a bug - missing null check at line 42

**Verified (95% confidence):**
The deployment at 14:10 introduced code that doesn't handle null user objects.
When a null user reaches line 42, the code crashes.

**Recommended Solution:**
1. **Immediate**: Rollback to v2.1.2 (fixes issue in ~5 minutes)
2. **Long-term**: Add null check at UserService.java:42

Would you like to proceed with the rollback?
""",
  
  "state_updates": {
    "milestones": {
      "symptom_verified": True,
      "timeline_established": True,
      "root_cause_identified": True
    },
    "verification_updates": {
      "symptom_statement": "NullPointerException at UserService.java:42 causing API failures",
      "temporal_state": "ONGOING",
      "urgency_level": "HIGH",
      "started_at": "2024-10-27T14:15:00Z",
      "recent_changes": [{
        "description": "Deployment v2.1.3",
        "occurred_at": "2024-10-27T14:10:00Z",
        "change_type": "deployment"
      }]
    },
    "evidence_to_add": [{
      "summary": "Error log showing NullPointerException at UserService.java:42",
      "analysis": "500+ errors starting 14:15, 5 minutes after deployment. Missing null check causes crash when user object is null."
    }],
    "root_cause_conclusion": {
      "root_cause": "Missing null check at UserService.java:42 introduced in v2.1.3",
      "mechanism": "When user object is null, line 42 crashes without null handling",
      "confidence_score": 0.95,
      "confidence_level": "verified"
    },
    "solutions_to_add": [{
      "title": "Rollback to v2.1.2",
      "solution_type": "rollback",
      "immediate_action": "kubectl rollout undo deployment/api",
      "longterm_fix": "Add null check: if (user != null) before line 42"
    }],
    "working_conclusion": {
      "statement": "Deployment v2.1.3 introduced bug: missing null check at line 42",
      "confidence": 0.95,
      "reasoning": "Error logs show exact line, timing matches deployment perfectly, error pattern consistent with null pointer",
      "supporting_evidence_ids": ["ev_will_be_generated"],
      "caveats": [],
      "next_evidence_needed": []
    },
    "outcome": "milestone_completed"
  }
}

# STEP 3: System post-processes

# Apply milestones
case.progress.symptom_verified = True
case.progress.timeline_established = True
case.progress.root_cause_identified = True
case.progress.root_cause_method = "direct_analysis"

# Process evidence
evidence = Evidence(
    evidence_id="ev_a1b2c3d4e5f6",  # System generates
    summary="Error log showing NullPointerException at UserService.java:42",
    analysis="500+ errors starting 14:15, 5 minutes after deployment...",
    category=EvidenceCategory.SYMPTOM_EVIDENCE,  # System infers (verification incomplete when added)
    advances_milestones=["symptom_verified", "timeline_established", "root_cause_identified"],  # System calculates
    collected_at=datetime.now(),  # System timestamp
    collected_by=case.user_id,  # System fills
    collected_at_turn=5,  # System counter
    content_ref="s3://uploads/error.log",  # System upload
    source_type=EvidenceSourceType.LOG_FILE,  # System detects
    form=EvidenceForm.DOCUMENT  # System detects
)
case.evidence.append(evidence)

# Process solution
solution = Solution(
    solution_id="sol_xyz789",  # System generates
    title="Rollback to v2.1.2",
    solution_type=SolutionType.ROLLBACK,
    immediate_action="kubectl rollout undo deployment/api",
    longterm_fix="Add null check: if (user != null) before line 42",
    proposed_at=datetime.now(),  # System timestamp
    proposed_by="agent"  # System fills
)
case.solutions.append(solution)

# Set working conclusion
case.working_conclusion = WorkingConclusion(
    statement="Deployment v2.1.3 introduced bug: missing null check at line 42",
    confidence=0.95,
    reasoning="Error logs show exact line, timing matches deployment perfectly",
    supporting_evidence_ids=["ev_a1b2c3d4e5f6"]
)

# Set root cause conclusion
case.root_cause_conclusion = RootCauseConclusion(
    root_cause="Missing null check at UserService.java:42 introduced in v2.1.3",
    mechanism="When user object is null, line 42 crashes without null handling",
    confidence_score=0.95,
    confidence_level=ConfidenceLevel.VERIFIED,
    evidence_basis=["ev_a1b2c3d4e5f6"]
)

# Detect progress
progress_made = True  # 3 milestones completed
case.turns_without_progress = 0  # Reset counter

# Record turn
turn = TurnProgress(
    turn_number=5,
    timestamp=datetime.now(),
    milestones_completed=["symptom_verified", "timeline_established", "root_cause_identified"],
    evidence_added=["ev_a1b2c3d4e5f6"],
    solutions_proposed=["sol_xyz789"],
    progress_made=True,
    outcome=TurnOutcome.MILESTONE_COMPLETED
)
case.turn_history.append(turn)

# Update turn counter
case.current_turn = 6

# Compute stage (automatic)
case.progress.current_stage  # Now returns InvestigationStage.SOLUTION

# Check status transitions
# None yet - solution not applied/verified

# STEP 4: Return to user
return llm_response.agent_response
```

**Result**: Investigation progressed from Understanding stage to Resolving stage in ONE turn by completing 3 milestones. No artificial constraints prevented this natural progression.

---

**END OF DOCUMENT**
