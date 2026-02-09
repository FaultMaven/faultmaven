# Conversational Interaction Model & Lifecycle Design

**Version:** 1.0
**Last Updated:** 2025-10-08
**Status:** Design Specification
**Authors:** FaultMaven Architecture Team

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [System Context](#system-context)
3. [Design Philosophy](#design-philosophy)
4. [Incident Resolution Lifecycle](#incident-resolution-lifecycle)
5. [Conversational Modes](#conversational-modes)
6. [Mode Detection & Transition System](#mode-detection--transition-system)
7. [State Machine Architecture](#state-machine-architecture)
8. [Data Models](#data-models)
9. [User Scenario Workflows](#user-scenario-workflows)
10. [API Contracts & Event Flows](#api-contracts--event-flows)
11. [Implementation Guidance](#implementation-guidance)
12. [Edge Cases & Error Handling](#edge-cases--error-handling)
13. [Success Metrics](#success-metrics)

---

## Executive Summary

### Purpose

This document defines the **Conversational Interaction Model** for FaultMaven - a comprehensive architecture that unifies three critical components:

1. **The 7-Phase Incident Resolution Lifecycle** - A structured progression from inquiry through post-incident documentation
2. **Four Distinct User Scenarios** - Technical Q&A, Investigation, Crisis Response, and Post-Incident Analysis
3. **Dual Conversational Modes** - Consultant Mode (reactive) and Lead Investigator Mode (proactive)

The design enables FaultMaven to adapt its behavior based on situational context while maintaining consistent, expert-level troubleshooting guidance.

### Key Innovations

1. **Dynamic Mode Switching**: Agent detects incident signals and requests permission to shift from reactive consultation to proactive investigation
2. **Lifecycle-Aware Conversations**: Agent understands where users are in the resolution journey and adapts methodology accordingly
3. **Evidence-Centric Progress**: Clear state persistence and phase transition triggers based on objective evidence collection
4. **Doctor-Patient Relationship**: Expert assistant that respects user autonomy while providing structured guidance

### Target Outcomes

- **User Clarity**: Users always understand what the agent is doing and why
- **Appropriate Urgency**: Fast iteration during incidents, deliberate analysis post-incident
- **Seamless Transitions**: Natural mode switches without jarring UX disruptions
- **Resumable Investigations**: Persistent state allows pausing and resuming complex troubleshooting
- **Knowledge Capture**: Automatic generation of runbooks and post-mortems for organizational learning

---

## System Context

### Existing Architecture Foundation

FaultMaven is built on three architectural pillars that this design extends:

#### 1. Evidence-Centric Troubleshooting System (v3.1.0)
- **Evidence Requests**: Structured requests with acquisition guidance
- **Multi-Dimensional Classification**: 5-dimension evidence analysis
- **Investigation Modes**: Active Incident vs Post-Mortem
- **Case Status Tracking**: 7-state lifecycle management

**Reference**: `/home/swhouse/projects/FaultMaven/docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md`

#### 2. Doctor/Patient Prompting Architecture
- **SRE 5-Phase Methodology**: Define Blast Radius → Timeline → Hypothesis → Validation → Solution
- **Sub-Agent System**: Specialized agents per phase (6 agents total)
- **Diagnostic State Tracking**: Server-side state maintained across conversations

**Models**: `/home/swhouse/projects/FaultMaven/faultmaven/models/doctor_patient.py`

#### 3. Case-Based Persistence
- **Case Model**: Persistent troubleshooting sessions with collaborative features
- **CaseDiagnosticState**: Server-side tracking of methodology progress
- **Message History**: Full conversation context with evidence tracking

**Models**: `/home/swhouse/projects/FaultMaven/faultmaven/models/case.py`

### Integration Points

This design **extends** existing architecture by:

1. **Mapping Lifecycle Phases** to existing SRE 5-phase methodology
2. **Defining Mode Transitions** within existing Investigation Mode framework
3. **Specifying Signal Detection** for automatic mode switching
4. **Detailing User Scenarios** as concrete workflow implementations

---

## Design Philosophy

### Core Principle: Doctor-Patient Relationship

FaultMaven operates as an **expert consultant**, not an autonomous agent:

- **User Retains Control**: Always asks permission before changing approach
- **Transparent Reasoning**: Explains what it's doing and why
- **Respectful Guidance**: Suggests but doesn't dictate
- **Adaptive Response**: Matches user urgency and complexity

### Conversational Design Tenets

#### 1. No Invisible State Changes
**Bad**: Agent silently switches to investigation mode
**Good**: "I notice this looks like an active incident. Would you like me to shift to structured troubleshooting to resolve this faster?"

#### 2. Explicit Phase Transitions
**Bad**: Agent moves to next phase automatically
**Good**: "We've identified the blast radius (3 services affected). Ready to establish the timeline of events?"

#### 3. Evidence Over Questions
**Bad**: "When did this start?"
**Good**: "Let's establish the timeline. Run `journalctl --since='2 hours ago' | grep ERROR` to check when errors began appearing."

#### 4. Mode-Appropriate Methodology
**Bad**: Same structured approach for casual questions and critical incidents
**Good**: Fast, mitigation-focused iteration during incidents; thorough, evidence-driven analysis post-incident

---

## Incident Resolution Lifecycle

### The 7-Phase Journey

The incident resolution lifecycle represents the **complete user journey** from first inquiry to organizational learning:

```
Phase 0 → Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6
Inquiry   Problem   Triage   Mitigation  Root      Solution  Documentation
          Definition         Recovery    Cause               Post-Mortem
```

#### Phase 0: Inquiry & Exploration (Steps 0-9)
**Purpose**: General Q&A, no active incident
**Agent Behavior**: Consultant Mode - answer questions, provide expertise
**Exit Condition**: User signals a problem OR agent detects incident indicators

**Example Interactions**:
- "What's the difference between Redis and Memcached?"
- "How do I configure rate limiting in NGINX?"
- "Tell me about circuit breaker patterns"

**Key Characteristics**:
- No evidence requests
- No phase progression
- Suggested actions focus on learning paths
- Can stay in Phase 0 indefinitely

#### Phase 1: Problem Definition & Impact Analysis (Steps 10-19)
**Purpose**: Understand the issue and scope the blast radius
**Agent Behavior**: Transition to Lead Investigator Mode (with permission)
**SRE Methodology Mapping**: Phase 0 (Intake) + Phase 1 (Blast Radius)

**Objectives**:
1. Confirm problem statement with user
2. Determine urgency level (normal/high/critical)
3. Identify affected systems/users (blast radius)
4. Set investigation mode (Active Incident vs Post-Mortem)

**Evidence Requests**:
- Error symptoms and manifestations
- Impact scope (how many users/services affected)
- Current system state

**Exit Condition**: Problem confirmed + blast radius defined

**State Transitions**:
```
CaseStatus: ACTIVE → INVESTIGATING
InvestigationMode: Set to ACTIVE_INCIDENT or POST_MORTEM
diagnostic_state.current_phase: 0 → 1
```

#### Phase 2: Triage & Correlation (Steps 20-29)
**Purpose**: Gather evidence and validate the problem exists
**Agent Behavior**: Systematic evidence collection
**SRE Methodology Mapping**: Phase 2 (Timeline)

**Objectives**:
1. Establish when the issue started
2. Identify recent changes (deployments, configs, code)
3. Correlate symptoms with timeline
4. Rule out false positives

**Evidence Requests**:
- Timeline data (when did it start?)
- Recent changes (what changed before symptoms?)
- Historical baselines (what's normal?)

**Exit Condition**: Timeline established + correlation identified

**State Transitions**:
```
diagnostic_state.current_phase: 1 → 2
evidence_requests: Timeline category marked complete
```

#### Phase 3: Mitigation & Service Recovery (Steps 30-39)
**Purpose**: Stop the bleeding, restore service availability
**Agent Behavior**: Action-focused, fast iteration
**Applies To**: Active Incident mode ONLY

**Objectives**:
1. Propose immediate mitigation actions
2. Guide service restoration
3. Implement temporary fixes
4. Validate service is back online

**Evidence Requests**:
- Mitigation action results
- Service health checks
- User-reported recovery

**Exit Condition**: Service restored OR mitigation failed (escalate)

**State Transitions**:
```
CaseStatus: INVESTIGATING → MITIGATED
InvestigationMode: ACTIVE_INCIDENT → POST_MORTEM (transition to RCA)
diagnostic_state.incident_mitigated: true
```

**Note**: Phase 3 is **skipped** in Post-Mortem mode (service already stable)

#### Phase 4: Root Cause Analysis (Steps 40-49)
**Purpose**: Understand the underlying cause
**Agent Behavior**: Deliberate, evidence-driven investigation
**SRE Methodology Mapping**: Phase 3 (Hypothesis) + Phase 4 (Validation)

**Objectives**:
1. Formulate hypotheses based on evidence
2. Test hypotheses systematically
3. Rule out alternative explanations
4. Identify definitive root cause

**Evidence Requests**:
- Configuration verification
- Code analysis
- Dependency health checks
- Historical incident patterns

**Exit Condition**: Root cause identified with sufficient confidence

**Confidence Requirements**:
- **Active Incident (now Mitigated)**: Confidence score optional, mitigation success is primary
- **Post-Mortem**: Confidence score ≥0.75 required for resolution

**State Transitions**:
```
diagnostic_state.current_phase: 3 → 4
diagnostic_state.root_cause: Set to identified cause
diagnostic_state.overall_confidence_score: Set (if POST_MORTEM)
```

#### Phase 5: Long-Term Solution (Steps 50-59)
**Purpose**: Design permanent fix and prevention measures
**Agent Behavior**: Solution architecture and implementation guidance
**SRE Methodology Mapping**: Phase 5 (Solution)

**Objectives**:
1. Design permanent fix (not workaround)
2. Identify prevention measures
3. Propose architectural improvements
4. Estimate implementation complexity

**Evidence Requests**:
- Implementation validation
- Testing results
- Deployment success

**Exit Condition**: Solution implemented and verified

**State Transitions**:
```
CaseStatus: MITIGATED → RESOLVED
diagnostic_state.solution_implemented: true
diagnostic_state.case_resolved: true
```

#### Phase 6: Documentation & Post-Mortem (Steps 60+)
**Purpose**: Capture learnings for organizational knowledge
**Agent Behavior**: Generate artifacts, facilitate reflection
**Deliverables**: Case Report + Runbook

**Objectives**:
1. Offer case report generation
2. Offer runbook creation (if new issue type)
3. Capture lessons learned
4. Update knowledge base

**Artifacts**:

**Case Report Contents**:
- Problem description and timeline
- Evidence collected
- Root cause analysis (with confidence score)
- Solution applied and validated
- Investigation dead ends (ruled out)
- Lessons learned

**Runbook Contents** (if new issue):
- Issue identification (symptoms, detection)
- Quick diagnosis steps
- Resolution procedure (mitigation + permanent fix)
- Prevention measures
- Escalation contacts

**Exit Condition**: User accepts or declines artifact generation

**State Transitions**:
```
CaseStatus: RESOLVED → CLOSED
diagnostic_state.case_report_url: Set (if generated)
diagnostic_state.runbook_url: Set (if generated)
```

### Phase Progression Rules

#### Forward Progression
- Phases advance when objectives are **objectively met** (not turn-based)
- Evidence completeness triggers progression
- User can override and skip phases (with agent warning)

#### Backward Movement
- **Not allowed** - phases represent completed work
- Exception: New evidence contradicts previous conclusions → agent proposes hypothesis revision (stays in same phase)

#### Phase Skipping
- **Phase 3 skipped** in Post-Mortem mode (service already stable)
- User can request skipping with agent acknowledgment
- Agent warns of potential gaps

---

## Conversational Modes

### Mode 1: Consultant Mode (Default)

**Character**: Expert colleague, reactive, supportive

**Behavior**:
- Answers questions directly
- Provides explanations and examples
- Offers suggested actions for exploration
- No structured methodology
- User drives conversation direction

**Response Pattern**:
```
Answer: [Direct answer to question]

Want to learn more?
• [Related topic 1]
• [Related topic 2]
• [Practical example]
```

**Use Cases**:
- Technical Q&A
- Learning/exploration
- Best practices inquiry
- Tool selection guidance

**Example Interaction**:
```
User: "What's the difference between horizontal and vertical scaling?"

Agent: "Horizontal scaling (scaling out) adds more servers to distribute load,
while vertical scaling (scaling up) increases resources on existing servers.

Horizontal scaling:
• Better for stateless applications
• Higher availability (no single point of failure)
• More complex (load balancing, distributed state)
• Example: Adding more web server instances

Vertical scaling:
• Simpler architecture
• Easier for stateful applications
• Limited by hardware maximums
• Example: Upgrading from 4GB to 16GB RAM

Want to explore more?
• Learn about auto-scaling strategies
• Compare cloud provider scaling options
• See real-world scaling examples"
```

### Mode 2: Lead Investigator Mode

**Character**: Diagnostic expert, proactive, structured

**Behavior**:
- Drives investigation with structured methodology
- Generates evidence requests with acquisition guidance
- Adapts urgency based on situation
- Proposes phase transitions explicitly
- Tracks progress toward resolution

**Response Pattern (Active Incident)**:
```
Analysis: [Current understanding]

Immediate Action:
1. [Mitigation step 1]
2. [Mitigation step 2]

Evidence needed:
• [Evidence request 1 with commands]
• [Evidence request 2 with commands]
```

**Response Pattern (Post-Mortem)**:
```
Current Hypothesis: [Working theory]

Evidence needed to validate:
• [Evidence request 1 with commands]
• [Evidence request 2 with commands]

Alternative explanations:
• [Alternative 1] - less likely because [reason]
```

**Use Cases**:
- Active incident troubleshooting
- Post-incident root cause analysis
- Systematic problem investigation

### Mode Comparison Table

| Aspect | Consultant Mode | Lead Investigator Mode |
|--------|----------------|----------------------|
| **User Control** | User drives | Agent guides (with permission) |
| **Structure** | Unstructured Q&A | Structured methodology |
| **Evidence** | Not tracked | Tracked and persistent |
| **Phase Progression** | None | Explicit phase transitions |
| **Urgency Adaptation** | Not applicable | Adapts to urgency level |
| **Suggested Actions** | Learning paths | Diagnostic commands |
| **Persistence** | Not critical | Critical (resumable) |
| **Exit Condition** | User stops asking | Investigation resolved or stalled |

---

## Mode Detection & Transition System

### Signal Detection Patterns

The agent continuously analyzes user input for **incident indicators**:

#### Strong Incident Signals (High Confidence)
- **Error Keywords**: "error", "failing", "broken", "down", "not working"
- **Urgency Language**: "urgent", "critical", "emergency", "production down"
- **Problem Statements**: "I have a problem", "something is wrong", "issue with"
- **Impact Indicators**: "users can't", "service unavailable", "data loss"

#### Moderate Incident Signals (Medium Confidence)
- **Symptom Descriptions**: "seeing weird behavior", "performance degraded"
- **Comparative Language**: "it used to work", "since yesterday", "after deployment"
- **Investigation Intent**: "trying to figure out", "debugging", "investigating"

#### Weak Signals (Low Confidence)
- **Vague Concerns**: "something seems off", "not sure what's happening"
- **Hypothetical Questions**: "what if X happened", "how would I debug Y"

### Mode Transition Protocol (3 Steps)

#### Step 1: Signal Detection
Agent detects incident signals and calculates confidence score:

```python
class IncidentSignalDetection(BaseModel):
    """Detection result for incident signals in user input"""

    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence this is an active problem (0-1)"
    )

    signals_detected: List[str] = Field(
        ...,
        description="Specific signals that triggered detection"
    )

    suggested_mode: ConversationalMode = Field(
        ...,
        description="Recommended mode based on signals"
    )

    urgency_estimate: UrgencyLevel = Field(
        ...,
        description="Estimated urgency (NORMAL/HIGH/CRITICAL)"
    )

    should_request_transition: bool = Field(
        ...,
        description="Whether agent should request mode switch"
    )
```

**Thresholds**:
- Confidence ≥ 0.7 → Request transition
- Confidence 0.4-0.69 → Mention possibility, don't request
- Confidence < 0.4 → Continue in current mode

#### Step 2: Permission Request
Agent explicitly asks user for mode change:

**Template (High Urgency)**:
```
🚨 I notice this sounds like an active incident affecting production.

Would you like me to shift into **structured troubleshooting mode**?

I'll guide you through:
1. Defining the problem and impact
2. Establishing the timeline
3. Mitigating the issue quickly
4. Finding the root cause

This will help us resolve faster. Want to proceed? (yes/no)
```

**Template (Normal Urgency)**:
```
I notice you're investigating an issue. Would you like structured guidance?

I can walk you through a systematic approach:
• Define the problem scope
• Establish timeline of events
• Formulate and test hypotheses
• Identify root cause

Continue casually or use structured approach? (structured/casual)
```

**Template (Post-Incident)**:
```
This sounds like a post-incident analysis. Would you like help with a thorough RCA?

I can guide you through:
• Collecting comprehensive evidence
• Testing all viable hypotheses
• Identifying definitive root cause (with confidence scoring)
• Generating case report and runbook

Want structured RCA? (yes/no)
```

#### Step 3: Confirm and Engage
User responds with confirmation:

**Affirmative Responses** (trigger transition):
- "yes", "ok", "sure", "go ahead", "let's do it"
- "structured", "help me", "guide me"
- Click on "Yes, use structured troubleshooting" button

**Negative Responses** (stay in Consultant Mode):
- "no", "not now", "just answer my question"
- "casual", "keep it simple"
- No response after 2 turns

**State Transition**:
```python
# Before transition
conversational_mode = ConversationalMode.CONSULTANT
has_active_problem = False

# After user confirms
conversational_mode = ConversationalMode.LEAD_INVESTIGATOR
has_active_problem = True
problem_statement = "[extracted from user input]"
urgency_level = [detected urgency]
investigation_mode = ACTIVE_INCIDENT or POST_MORTEM
current_phase = 1  # Enter Phase 1: Problem Definition
```

### Reverse Transition (Lead Investigator → Consultant)

Users can exit structured mode at any time:

**User Signals**:
- Explicit: "stop", "cancel", "go back to casual mode"
- Implicit: Off-topic questions, disengagement, long pauses

**Agent Detection**:
```python
def detect_disengagement(
    recent_messages: List[Message],
    evidence_response_rate: float,
    turns_without_progress: int
) -> bool:
    """Detect if user is disengaging from structured investigation"""

    # User not responding to evidence requests
    if evidence_response_rate < 0.3 and turns_without_progress >= 3:
        return True

    # User asking off-topic questions
    off_topic_count = sum(1 for m in recent_messages[-3:]
                         if m.user_intent == UserIntent.OFF_TOPIC)
    if off_topic_count >= 2:
        return True

    return False
```

**Agent Response**:
```
I notice we haven't made progress in a few turns. Would you like to:

• Continue structured investigation (I'll adjust approach)
• Pause investigation for now (we can resume later)
• Switch back to casual Q&A mode

What works best for you?
```

### Mode Persistence

**Session Continuity**:
- Mode persists within a case (even across sessions)
- Evidence requests and progress preserved
- Note: Terminal states (RESOLVED/CLOSED) are irreversible; create new case for recurrence

**State Storage**:
```python
class CaseConversationalState(BaseModel):
    """Persistent conversational state for a case"""

    conversational_mode: ConversationalMode
    mode_transition_history: List[ModeTransition]
    last_mode_change_turn: int
    user_explicitly_set_mode: bool  # vs agent suggested
```

---

## State Machine Architecture

### Overall System State Machine

```
┌─────────────────────────────────────────────────────────────────┐
│                      CONSULTANT MODE                             │
│                   (Phase 0: Inquiry)                             │
│                                                                   │
│  User asks questions → Agent answers → Suggested learning paths  │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     │ Incident Signal Detected
                     │ (Confidence ≥ 0.7)
                     ▼
              ┌─────────────┐
              │   REQUEST   │
              │ PERMISSION  │
              └──────┬──────┘
                     │
          ┌──────────┴──────────┐
          │                     │
      User Says            User Says
        "No"                "Yes"
          │                     │
          ▼                     ▼
    ┌──────────┐      ┌────────────────┐
    │  STAY IN │      │  TRANSITION TO │
    │CONSULTANT│      │LEAD INVESTIGATOR│
    │   MODE   │      └───────┬────────┘
    └──────────┘              │
                              ▼
    ┌─────────────────────────────────────────────────────────────┐
    │              LEAD INVESTIGATOR MODE                          │
    │                                                               │
    │  Phase 1: Problem Definition                                 │
    │    ↓                                                          │
    │  Phase 2: Triage & Correlation                               │
    │    ↓                                                          │
    │  Phase 3: Mitigation (ACTIVE_INCIDENT only)                  │
    │    ↓                                                          │
    │  Phase 4: Root Cause Analysis                                │
    │    ↓                                                          │
    │  Phase 5: Long-Term Solution                                 │
    │    ↓                                                          │
    │  Phase 6: Documentation & Post-Mortem                        │
    └───────────────────────────┬─────────────────────────────────┘
                                │
                                │ Case Closed
                                ▼
                        ┌───────────────┐
                        │  CASE CLOSED  │
                        │ (Can reopen)  │
                        └───────────────┘
```

### Phase Transition State Machine (Lead Investigator Mode)

```
   ┌────────────────────────────────────────────────────────┐
   │  Phase 1: Problem Definition & Impact Analysis         │
   │                                                          │
   │  Entry: User confirms mode switch                       │
   │  Objectives:                                            │
   │    • Confirm problem statement                          │
   │    • Define blast radius                                │
   │    • Set urgency level                                  │
   │    • Determine investigation mode                       │
   │  Exit: Problem confirmed + blast radius defined         │
   └────────────────────┬───────────────────────────────────┘
                        │ Objectives Met
                        │ Agent: "Ready to establish timeline?"
                        ▼
   ┌────────────────────────────────────────────────────────┐
   │  Phase 2: Triage & Correlation                          │
   │                                                          │
   │  Entry: Blast radius defined                            │
   │  Objectives:                                            │
   │    • Establish when issue started                       │
   │    • Identify recent changes                            │
   │    • Correlate symptoms with timeline                   │
   │  Exit: Timeline established + correlation found         │
   └────────────────────┬───────────────────────────────────┘
                        │ Timeline Complete
                        │
         ┌──────────────┴──────────────┐
         │                             │
   InvestigationMode          InvestigationMode
   = ACTIVE_INCIDENT          = POST_MORTEM
         │                             │
         ▼                             │ (Skip Phase 3)
   ┌─────────────────────┐            │
   │  Phase 3:           │            │
   │  Mitigation &       │            │
   │  Service Recovery   │            │
   │                     │            │
   │  Entry: Timeline    │            │
   │    complete         │            │
   │  Objectives:        │            │
   │    • Propose        │            │
   │      mitigation     │            │
   │    • Restore        │            │
   │      service        │            │
   │    • Validate       │            │
   │      recovery       │            │
   │  Exit: Service      │            │
   │    restored         │            │
   └──────┬──────────────┘            │
          │ Service Restored          │
          │ (Transition to POST_MORTEM)│
          └──────────┬──────────────────┘
                     │
                     ▼
   ┌────────────────────────────────────────────────────────┐
   │  Phase 4: Root Cause Analysis                           │
   │                                                          │
   │  Entry: Service stable (mitigated or always stable)     │
   │  Objectives:                                            │
   │    • Formulate hypotheses                               │
   │    • Test systematically                                │
   │    • Rule out alternatives                              │
   │    • Identify root cause                                │
   │  Exit: Root cause identified (confidence-based)         │
   │                                                          │
   │  Confidence Requirements:                               │
   │    • POST_MORTEM: ≥0.75 required                        │
   │    • ACTIVE_INCIDENT (now MITIGATED): optional          │
   └────────────────────┬───────────────────────────────────┘
                        │ Root Cause Found
                        ▼
   ┌────────────────────────────────────────────────────────┐
   │  Phase 5: Long-Term Solution                            │
   │                                                          │
   │  Entry: Root cause identified                           │
   │  Objectives:                                            │
   │    • Design permanent fix                               │
   │    • Identify prevention measures                       │
   │    • Estimate implementation                            │
   │  Exit: Solution implemented and verified                │
   └────────────────────┬───────────────────────────────────┘
                        │ Solution Implemented
                        ▼
   ┌────────────────────────────────────────────────────────┐
   │  Phase 6: Documentation & Post-Mortem                   │
   │                                                          │
   │  Entry: Solution implemented                            │
   │  Objectives:                                            │
   │    • Offer case report generation                       │
   │    • Offer runbook creation                             │
   │    • Capture lessons learned                            │
   │  Exit: User accepts/declines artifacts                  │
   └────────────────────┬───────────────────────────────────┘
                        │ Artifacts Handled
                        ▼
                   ┌──────────┐
                   │  CLOSED  │
                   └──────────┘
```

### Investigation Mode State Machine

```
                    ┌──────────────────┐
                    │  MODE SELECTION  │
                    │  (Phase 1)       │
                    └────────┬─────────┘
                             │
         ┌───────────────────┴───────────────────┐
         │                                       │
    Problem Active                         Problem Historical
    (service down, ongoing)                (after resolution)
         │                                       │
         ▼                                       ▼
┌──────────────────────┐              ┌────────────────────┐
│  ACTIVE_INCIDENT     │              │  POST_MORTEM       │
│                      │              │                    │
│  Priority: Speed     │              │  Priority: Depth   │
│  Confidence: Optional│              │  Confidence: ≥0.75 │
│  Escalation: 3 fails │              │  Escalation: Rare  │
└──────────┬───────────┘              └──────────┬─────────┘
           │                                     │
           │ Service Restored                    │
           │ (Phase 3 Complete)                  │
           │                                     │
           └──────────┬────────────────────────┘
                      │
                      ▼
          ┌────────────────────────┐
          │  POST_MORTEM (RCA)     │
          │                        │
          │  Continues in Phase 4  │
          │  with thoroughness     │
          └────────────────────────┘
```

### Evidence Request Lifecycle State Machine

```
┌──────────┐
│ PENDING  │  Evidence requested, waiting for user
└────┬─────┘
     │
     │ User provides partial information
     ▼
┌──────────┐
│ PARTIAL  │  Some info received, need more
└────┬─────┘
     │
     │ User provides complete information
     ▼
┌──────────┐
│ COMPLETE │  Request fully satisfied
└──────────┘

Alternative paths from PENDING:

┌──────────┐
│ PENDING  │
└────┬─────┘
     │ User reports cannot access
     ▼
┌──────────┐
│ BLOCKED  │  Cannot provide (access/availability)
└──────────┘

┌──────────┐
│ PENDING  │
└────┬─────┘
     │ Investigation moves past need
     ▼
┌──────────┐
│ OBSOLETE │  No longer relevant
└──────────┘
```

---

## Data Models

### Core State Models

#### ConversationalMode

```python
class ConversationalMode(str, Enum):
    """Two interaction modes for the agent"""
    CONSULTANT = "consultant"           # Reactive Q&A mode
    LEAD_INVESTIGATOR = "lead_investigator"  # Proactive structured troubleshooting
```

#### LifecyclePhase

```python
class LifecyclePhase(int, Enum):
    """7-phase incident resolution lifecycle"""
    INQUIRY = 0           # General Q&A (no active problem)
    PROBLEM_DEFINITION = 1  # Define problem + blast radius
    TRIAGE = 2             # Timeline + correlation
    MITIGATION = 3         # Service recovery (ACTIVE_INCIDENT only)
    ROOT_CAUSE = 4         # Hypothesis + validation
    SOLUTION = 5           # Permanent fix design
    DOCUMENTATION = 6      # Post-mortem + artifacts
```

#### CaseConversationalState

```python
class ModeTransition(BaseModel):
    """Record of mode change"""
    from_mode: ConversationalMode
    to_mode: ConversationalMode
    turn_number: int
    trigger: str = Field(..., description="What triggered the transition")
    user_initiated: bool = Field(..., description="True if user requested, False if agent suggested")
    timestamp: datetime


class CaseConversationalState(BaseModel):
    """Extended conversational state for lifecycle management"""

    # Mode tracking
    conversational_mode: ConversationalMode = ConversationalMode.CONSULTANT
    lifecycle_phase: LifecyclePhase = LifecyclePhase.INQUIRY

    # Mode transition history
    mode_transitions: List[ModeTransition] = Field(default_factory=list)
    last_mode_change_turn: int = 0
    user_explicitly_set_mode: bool = False

    # Permission tracking
    pending_mode_transition: Optional[ConversationalMode] = None
    awaiting_mode_permission: bool = False
    permission_request_turn: Optional[int] = None

    # Phase progression tracking
    phase_objectives_met: Dict[int, bool] = Field(
        default_factory=dict,
        description="Map of phase number to completion status"
    )
    phase_entry_turn: Dict[int, int] = Field(
        default_factory=dict,
        description="Map of phase number to turn when entered"
    )
    last_phase_transition_turn: int = 0

    # Investigation mode (from existing evidence-centric design)
    investigation_mode: InvestigationMode = InvestigationMode.ACTIVE_INCIDENT

    # Disengagement detection
    evidence_response_rate: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Rate of user responses to evidence requests"
    )
    off_topic_turns: int = 0
    turns_without_progress: int = 0

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat() + 'Z'}
```

#### IncidentSignalDetection

```python
class IncidentSignalDetection(BaseModel):
    """Result of analyzing user input for incident indicators"""

    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence this is an active problem (0-1)"
    )

    signals_detected: List[str] = Field(
        default_factory=list,
        description="Specific signals found in user input"
    )

    signal_strengths: Dict[str, float] = Field(
        default_factory=dict,
        description="Map of signal type to strength score"
    )

    suggested_mode: ConversationalMode = Field(
        ...,
        description="Recommended mode based on signals"
    )

    urgency_estimate: UrgencyLevel = Field(
        ...,
        description="Estimated urgency (NORMAL/HIGH/CRITICAL)"
    )

    problem_type: Optional[str] = Field(
        None,
        description="Classified problem type (investigation, incident, post-mortem)"
    )

    should_request_transition: bool = Field(
        ...,
        description="Whether agent should request mode switch"
    )

    rationale: str = Field(
        ...,
        description="Why this detection was made"
    )
```

#### PhaseTransitionProposal

```python
class PhaseTransitionProposal(BaseModel):
    """Agent's proposal to advance to next phase"""

    current_phase: LifecyclePhase
    proposed_phase: LifecyclePhase

    objectives_completed: List[str] = Field(
        ...,
        description="What objectives were met in current phase"
    )

    next_phase_objectives: List[str] = Field(
        ...,
        description="What will be accomplished in next phase"
    )

    rationale: str = Field(
        ...,
        description="Why ready to advance"
    )

    user_confirmation_required: bool = Field(
        default=True,
        description="Whether user must approve transition"
    )

    can_skip: bool = Field(
        default=False,
        description="Whether this phase can be skipped"
    )
```

### Extended Existing Models

#### CaseDiagnosticState (Extensions)

Add to existing `/home/swhouse/projects/FaultMaven/faultmaven/models/case.py`:

```python
class CaseDiagnosticState(BaseModel):
    """Extended with lifecycle management fields"""

    # ... existing fields ...

    # NEW: Conversational state
    conversational_state: CaseConversationalState = Field(
        default_factory=CaseConversationalState,
        description="Conversational mode and lifecycle tracking"
    )

    # NEW: Phase transition proposals
    pending_phase_transition: Optional[PhaseTransitionProposal] = None

    # NEW: Methodology adaptation
    methodology_adjustments: Dict[str, Any] = Field(
        default_factory=dict,
        description="Mode-specific methodology adjustments"
    )
```

---

## User Scenario Workflows

### Scenario 1: Technical Q&A (Phase 0 Only)

**User Profile**: Engineer learning about technologies, no active problem

**Workflow**:

```
User: "What's the difference between Redis and Memcached?"

┌─────────────────────────────────────┐
│ Agent Analysis:                      │
│ • Mode: CONSULTANT                   │
│ • Phase: INQUIRY (0)                 │
│ • Incident Signals: None detected    │
│ • Action: Answer question directly   │
└─────────────────────────────────────┘

Agent Response:
"Redis and Memcached are both in-memory data stores, but they differ in key ways:

**Redis:**
• Supports complex data structures (lists, sets, sorted sets, hashes)
• Built-in persistence options (RDB snapshots, AOF logs)
• Pub/sub messaging
• Lua scripting
• Single-threaded but uses I/O multiplexing

**Memcached:**
• Simple key-value storage only
• No persistence (pure cache)
• Multi-threaded
• Simpler, lower memory overhead
• Faster for pure caching workloads

Choose Memcached if you need a simple, fast cache.
Choose Redis if you need:
• Data structure operations
• Persistence
• Pub/sub functionality

Want to explore more?
• Compare performance benchmarks
• Learn about Redis clustering
• See example use cases"

┌─────────────────────────────────────┐
│ State Update:                        │
│ • conversational_mode: CONSULTANT    │
│ • lifecycle_phase: INQUIRY           │
│ • No evidence requests               │
│ • No phase progression               │
└─────────────────────────────────────┘
```

**Key Characteristics**:
- Stays in Phase 0 indefinitely
- No mode transition triggers
- Can span multiple questions
- No state persistence critical

---

### Scenario 2: "Something's Weird" Investigation

**User Profile**: SRE noticing odd behavior, wants to investigate before it escalates

**Workflow**:

```
Turn 1:
User: "I'm seeing some weird latency spikes in our API, not sure what's causing it"

┌─────────────────────────────────────┐
│ Signal Detection:                    │
│ • Confidence: 0.75                   │
│ • Signals:                           │
│   - "weird" (moderate indicator)     │
│   - "latency spikes" (symptom)       │
│   - "not sure what's causing"        │
│ • Urgency: NORMAL                    │
│ • Suggested: LEAD_INVESTIGATOR       │
│ • Request Transition: YES            │
└─────────────────────────────────────┘

Agent Response:
"I notice you're investigating an issue with API latency. Would you like structured guidance?

I can walk you through a systematic approach:
• Define the problem scope (how often, which endpoints)
• Establish timeline of events
• Formulate and test hypotheses
• Identify root cause

Continue casually or use structured approach?

[Structured Approach] [Keep it Casual]"

Turn 2:
User: [Clicks "Structured Approach"]

┌─────────────────────────────────────┐
│ Mode Transition:                     │
│ • From: CONSULTANT                   │
│ • To: LEAD_INVESTIGATOR              │
│ • Phase: INQUIRY → PROBLEM_DEFINITION│
│ • Investigation Mode: POST_MORTEM    │
│   (not critical, service is up)      │
└─────────────────────────────────────┘

Agent Response:
"Great! Let's investigate this systematically.

🎯 Current Understanding:
• Symptom: Latency spikes in API
• Impact: Unknown (need to quantify)
• Urgency: Normal

Let me confirm - is the API:
✓ Still functioning (just slower)?
✓ Affecting all endpoints or specific ones?
✓ Impacting all users or subset?

Evidence needed to scope the problem:

1️⃣ **Latency metrics baseline**
   What we need: Current vs normal latency

   Commands:
   • curl http://your-monitoring/api/latency?timerange=24h
   • kubectl top pods -n production (check resource usage)

   UI:
   • Datadog > API Dashboard > Latency Graph

   Expected: Compare current (e.g., p95: 800ms) vs baseline (p95: 200ms)

2️⃣ **Affected scope**
   What we need: Which endpoints showing spikes

   Commands:
   • grep 'response_time' /var/log/api.log | awk '$3 > 500'

   UI:
   • APM > Endpoint Performance Breakdown

   Expected: List of slow endpoints (if all = systemic, if few = localized)

What can you check first?"

Turn 3:
User: "Checked monitoring - p95 latency is 850ms vs usual 200ms. Seems to affect all endpoints."

┌─────────────────────────────────────┐
│ Evidence Classification:             │
│ • Matched Requests: [req-001, req-002]│
│ • Completeness: OVER_COMPLETE        │
│ • Evidence Type: SUPPORTIVE          │
│ • Phase 1 Objectives:                │
│   ✓ Blast radius: All endpoints      │
│   ✓ Impact quantified: 4x increase   │
│   ✓ Urgency confirmed: NORMAL        │
└─────────────────────────────────────┘

Agent Response:
"Excellent - we have a clear problem definition:

📊 **Confirmed Problem**:
• 4x latency increase (200ms → 850ms p95)
• Systemic issue (all endpoints affected)
• Service still functional (investigating, not mitigating)

✅ **Phase 1 Complete: Problem Scoped**

Ready to establish the timeline? This will help us identify:
• When spikes started
• What changed before symptoms
• Correlation with deployments/configs

Proceed to timeline analysis?

[Yes, let's continue] [Pause here]"

Turn 4:
User: [Clicks "Yes, let's continue"]

┌─────────────────────────────────────┐
│ Phase Transition:                    │
│ • From: PROBLEM_DEFINITION (1)       │
│ • To: TRIAGE (2)                     │
│ • Objectives Met: Yes                │
│ • User Confirmed: Yes                │
└─────────────────────────────────────┘

Agent Response:
"🕐 Phase 2: Timeline Establishment

Let's figure out when this started and what changed.

Evidence needed:

1️⃣ **When did latency increase start?**

   Commands:
   • journalctl --since='3 days ago' | grep -i 'slow\|latency\|timeout'

   UI:
   • Monitoring > Latency Graph > Zoom to 72h view

   Expected: Timestamp when latency jumped (look for sudden spike)

2️⃣ **Recent changes before spike**

   Commands:
   • git log --since='3 days ago' --oneline (check recent deployments)
   • kubectl rollout history deployment/api

   Expected: Deployments, config changes, database migrations

3️⃣ **External factors**

   What changed:
   • Traffic increase? (check request volume)
   • Database issues? (check DB latency)
   • Network changes? (check infrastructure logs)

What can you check?"

... [Continue with timeline → hypothesis → validation → solution]

Final Turn (Phase 6):
Agent Response:
"🎉 Issue Resolved!

**Root Cause**: Database query added in v2.3.1 missing index
**Solution**: Created index on users.last_login_at
**Result**: Latency back to 210ms p95

Would you like me to generate:

📋 **Case Report**
• Complete investigation timeline
• Evidence collected
• Root cause analysis (confidence: 0.92)
• Solution implemented

📖 **Runbook** (New)
• How to detect similar issues
• Quick diagnosis steps
• Resolution procedure
• Prevention measures

Generate artifacts?

[Both] [Report Only] [No Thanks]"

```

**Key Characteristics**:
- Smooth transition from casual to structured
- User explicitly confirms mode switch
- Agent proposes phase transitions
- Persistent state allows resuming
- Ends with artifacts for learning

---

### Scenario 3: "House is on Fire" Crisis Response

**User Profile**: On-call engineer, production service down, high pressure

**Workflow**:

```
Turn 1:
User: "URGENT: Our payment API is completely down! Users can't checkout!"

┌─────────────────────────────────────┐
│ Signal Detection:                    │
│ • Confidence: 0.98                   │
│ • Signals:                           │
│   - "URGENT" (critical keyword)      │
│   - "completely down" (service down) │
│   - "can't checkout" (business impact)│
│ • Urgency: CRITICAL                  │
│ • Suggested: LEAD_INVESTIGATOR       │
│ • Investigation Mode: ACTIVE_INCIDENT│
└─────────────────────────────────────┘

Agent Response:
"🚨 **CRITICAL INCIDENT DETECTED**

I understand - payment API down means revenue impact. Let me help you resolve this fast.

I'll guide you through:
1. **Define Impact** (30 seconds)
2. **Quick Mitigation** (try to restore service)
3. **Root Cause** (after service restored)

Ready for structured emergency response?

[YES - START NOW] [No, I'll handle it]"

Turn 2:
User: [Clicks "YES - START NOW"]

┌─────────────────────────────────────┐
│ Mode Transition (Immediate):         │
│ • Mode: LEAD_INVESTIGATOR            │
│ • Phase: PROBLEM_DEFINITION (1)      │
│ • Investigation: ACTIVE_INCIDENT     │
│ • Urgency: CRITICAL                  │
│ • Methodology: FAST ITERATION        │
└─────────────────────────────────────┘

Agent Response:
"⚡ **IMMEDIATE ACTION MODE**

**Problem**: Payment API down, checkout broken
**Impact**: All users (revenue loss)

🔴 **STEP 1: Quick Health Check** (30 seconds)

Run NOW:
```bash
# Check if API pods are running
kubectl get pods -n production -l app=payment-api

# Check recent restarts
kubectl get pods -n production -l app=payment-api --field-selector=status.phase=Running
```

Expected: See pod status (Running/CrashLoopBackOff/etc)

**While you run that**, tell me:
• When did it go down? (exact time if possible)
• Any deployments today?

GO - run those commands!"

Turn 3:
User: "Pods are in CrashLoopBackOff. Deployment was 15 minutes ago."

┌─────────────────────────────────────┐
│ Evidence Classification:             │
│ • Matched: req-001 (health check)    │
│ • Completeness: COMPLETE             │
│ • Evidence Type: SUPPORTIVE          │
│ • Timeline: Correlated with deploy   │
│ • Phase 1 Complete: YES              │
│ • SKIP Phase 2 (timeline clear)      │
│ • PROCEED to Phase 3 (MITIGATION)    │
└─────────────────────────────────────┘

Agent Response:
"✅ **ROOT CAUSE LIKELY: BAD DEPLOYMENT**

CrashLoopBackOff + recent deploy = deployment issue

🚀 **MITIGATION: ROLLBACK** (30 seconds)

Run NOW:
```bash
# Rollback to previous version
kubectl rollout undo deployment/payment-api -n production

# Watch recovery
kubectl rollout status deployment/payment-api -n production
```

This will:
✓ Restore previous working version
✓ Should restore service in ~60 seconds

**RUN IT NOW!** Then report back if service is restored.

(We'll do RCA after service is up)"

Turn 4:
User: "Rollback complete! Service is back up, payments working!"

┌─────────────────────────────────────┐
│ State Transition:                    │
│ • Phase: MITIGATION (3) → RCA (4)    │
│ • CaseStatus: INVESTIGATING → MITIGATED│
│ • Investigation: ACTIVE_INCIDENT →   │
│                  POST_MORTEM         │
│ • incident_mitigated: true           │
│ • mitigation_actions: ["rollback"]   │
└─────────────────────────────────────┘

Agent Response:
"🎉 **SERVICE RESTORED!**

✅ Payment API operational
✅ Checkouts working
⏱️ Downtime: ~18 minutes

📋 **What we did**:
• Identified: CrashLoopBackOff from bad deployment
• Mitigation: Rollback to previous version
• Result: Service restored

---

Now that the fire is out, want to understand **why** the deployment failed?

I can help you:
• Find the exact failure (check pod logs)
• Identify bad change (git diff comparison)
• Prevent recurrence (add health checks, staging validation)

Continue to Root Cause Analysis?

[Yes - Find the Bug] [No - I'll investigate later]"

Turn 5:
User: [Clicks "Yes - Find the Bug"]

┌─────────────────────────────────────┐
│ Phase Transition:                    │
│ • Phase: TRIAGE (2) → ROOT_CAUSE (4) │
│   (skipped Phase 2 due to urgency)   │
│ • Investigation Mode: POST_MORTEM    │
│ • Methodology: THOROUGH ANALYSIS     │
│ • Confidence Required: ≥0.75         │
└─────────────────────────────────────┘

Agent Response:
"🔍 **Root Cause Analysis** (No rush - service is stable)

Let's find out what broke in that deployment.

Evidence needed:

1️⃣ **Pod crash logs**

   Commands:
   ```bash
   kubectl logs deployment/payment-api -n production --previous
   kubectl describe pod <pod-name> -n production
   ```

   Expected: Error messages, stack traces, exit codes

2️⃣ **What changed in deployment**

   Commands:
   ```bash
   git diff HEAD~1 HEAD
   kubectl diff -f k8s/payment-api-deployment.yaml
   ```

   Expected: Code changes, config changes

3️⃣ **Environment issues**

   Check:
   • New environment variables?
   • Database migrations?
   • Dependency changes?

What do the crash logs show?"

... [Continue with RCA → Solution → Documentation]
```

**Key Characteristics**:
- **Immediate mode switch** (crisis urgency)
- **Fast iteration** - one evidence request at a time
- **Phase skipping** - Skip Phase 2 when timeline is obvious
- **Mode transition** - ACTIVE_INCIDENT → POST_MORTEM after mitigation
- **Methodology change** - Fast mitigation first, thorough RCA second

---

### Scenario 4: Post-Incident RCA (Starts at Phase 4)

**User Profile**: Engineer investigating yesterday's incident, wants thorough analysis

**Workflow**:

```
Turn 1:
User: "I want to understand what caused yesterday's database outage. Service is stable now."

┌─────────────────────────────────────┐
│ Signal Detection:                    │
│ • Confidence: 0.85                   │
│ • Signals:                           │
│   - "what caused" (RCA intent)       │
│   - "yesterday's" (historical)       │
│   - "stable now" (post-incident)     │
│ • Suggested: LEAD_INVESTIGATOR       │
│ • Investigation: POST_MORTEM         │
│ • Entry Phase: ROOT_CAUSE (4)        │
│   (skip Phases 1-3, incident resolved)│
└─────────────────────────────────────┘

Agent Response:
"This sounds like a post-incident analysis. Would you like help with a thorough RCA?

I can guide you through:
• Collecting comprehensive evidence
• Testing all viable hypotheses
• Identifying definitive root cause (with confidence scoring)
• Generating case report and runbook

Since the incident is resolved, we can be thorough. Want structured RCA?

[Yes - Deep Dive RCA] [No - Just answer questions]"

Turn 2:
User: [Clicks "Yes - Deep Dive RCA"]

┌─────────────────────────────────────┐
│ Mode Transition:                     │
│ • Mode: LEAD_INVESTIGATOR            │
│ • Phase: INQUIRY → ROOT_CAUSE (4)    │
│   (Skip Phases 1-3, incident over)   │
│ • Investigation: POST_MORTEM         │
│ • Methodology: DEPTH OVER SPEED      │
│ • Confidence Required: ≥0.75         │
└─────────────────────────────────────┘

Agent Response:
"🔍 **Post-Mortem Root Cause Analysis**

Since service is stable, we can investigate thoroughly.

📋 **What I understand so far**:
• Incident: Database outage yesterday
• Current state: Service stable now
• Goal: Understand root cause completely

Let's gather comprehensive evidence. We'll test multiple hypotheses systematically.

**Evidence Collection - Phase 1: Incident Context**

1️⃣ **Incident timeline and symptoms**

   What we need:
   • When did database go down? (exact timestamp)
   • What were the symptoms? (connection timeouts, query errors, etc)
   • When was service restored? (timeline duration)

   Sources:
   • Incident report or tickets
   • Monitoring dashboard screenshots
   • Chat logs from incident response

2️⃣ **Database state during incident**

   Commands:
   ```bash
   # Historical database logs (if retained)
   journalctl -u postgresql --since='yesterday 00:00' --until='yesterday 23:59'

   # Check if metrics are retained
   curl http://monitoring/db-metrics?date=yesterday
   ```

   Expected: Connection count, query latency, error rates, resource usage

3️⃣ **Events before outage**

   What changed in 24h before outage:
   • Application deployments
   • Database configuration changes
   • Infrastructure changes (scaling, migrations)
   • Traffic patterns (sudden spike?)

Take your time - provide as much detail as possible."

Turn 3:
User: "Database went down at 14:23 UTC. Connection timeouts started. Restored at 15:47 (84 min downtime). No deployments that day. Found this in logs: 'max_connections reached (100)'"

┌─────────────────────────────────────┐
│ Evidence Classification:             │
│ • Matched: req-001, req-002          │
│ • Completeness: OVER_COMPLETE        │
│ • Key Finding: max_connections=100   │
│ • Initial Hypothesis: Connection leak│
└─────────────────────────────────────┘

Agent Response:
"📊 **Evidence Analysis**

Key findings:
✓ Duration: 84 minutes (14:23 - 15:47 UTC)
✓ Symptom: Connection timeouts
✓ Root log: `max_connections reached (100)`
✓ No recent deployments

🤔 **Initial Hypothesis**: Connection pool exhaustion

**Working Theory**:
Application failed to close connections → pool leaked → hit 100 limit → new connections rejected

**Confidence**: 0.45 (needs validation)

**What would increase confidence**:

1️⃣ **Connection growth pattern**

   Commands:
   ```bash
   # Connection count over time (if metrics retained)
   SELECT date_trunc('minute', timestamp),
          COUNT(*) as active_connections
   FROM pg_stat_activity_history
   WHERE timestamp > '2025-10-07 13:00'
   GROUP BY 1 ORDER BY 1;
   ```

   Expected: See connections gradually increase to 100

2️⃣ **Application connection handling**

   Check:
   • Connection pool configuration (max pool size)
   • Are connections being closed properly?
   • Any long-running queries?

3️⃣ **Alternative explanations to rule out**

   Could it be:
   • Traffic spike? (check request volume)
   • Slow queries? (check query execution times)
   • Database resource limits? (CPU, memory, disk)

What do you find?"

Turn 4:
User: "Checked metrics - connection count grew steadily from 20 to 100 over 2 hours. App pool size is 200 (way higher than DB limit). Found slow query that takes 5 minutes."

┌─────────────────────────────────────┐
│ Evidence Update:                     │
│ • New Finding: Slow query (5 min)    │
│ • New Finding: App pool=200, DB=100  │
│ • Hypothesis Refined: Connection     │
│   starvation from slow queries       │
│ • Confidence: 0.75 → 0.85            │
└─────────────────────────────────────┘

Agent Response:
"💡 **Root Cause Identified**

🎯 **Root Cause**: Connection starvation from slow queries + config mismatch

**Complete Picture**:
1. App configured for 200 connections
2. Database max_connections = 100 (misconfiguration)
3. Slow query (5 min) holds connections open
4. Connections accumulated over 2 hours (20 → 100)
5. Once at 100, new connections rejected
6. Cascading failure

**Confidence**: 0.85 (High)

**Supporting Evidence**:
✓ Steady connection growth to 100 (not sudden spike)
✓ Log: max_connections reached
✓ App pool (200) > DB limit (100) - mismatch
✓ Slow query holds connections open
✓ No traffic spike (rules out load issue)

**Missing Evidence** (would increase to 0.95):
✗ Exact slow query identification
✗ Why query became slow (code change? data growth?)

**Alternative Explanations** (ruled out):
❌ Traffic spike - No evidence in request volume
❌ Connection leak - Connections were closed (just slowly)
❌ Resource exhaustion - CPU/memory normal

---

Ready to design the permanent fix?"

Turn 5:
User: "Yes, what's the fix?"

┌─────────────────────────────────────┐
│ Phase Transition:                    │
│ • Phase: ROOT_CAUSE (4) → SOLUTION (5)│
│ • Root cause confirmed: YES          │
│ • Confidence: 0.85 (meets ≥0.75)     │
└─────────────────────────────────────┘

Agent Response:
"🔧 **Phase 5: Long-Term Solution**

**Three-Part Fix** (priority order):

**1. Immediate: Fix Configuration Mismatch** (High Priority)
```sql
-- Increase database max_connections
ALTER SYSTEM SET max_connections = 250;
SELECT pg_reload_conf();
```
Why: App pool (200) must be < DB limit (250) with buffer

**2. Short-term: Optimize Slow Query** (High Priority)
```sql
-- Add missing index (if query is SELECT with WHERE clause)
CREATE INDEX idx_table_column ON table_name(column_name);

-- Or add query timeout
ALTER DATABASE your_db SET statement_timeout = '30s';
```
Why: 5-minute queries are never acceptable

**3. Long-term: Add Connection Pool Monitoring** (Medium Priority)
```yaml
# Add alert when connections > 80%
alert: DatabaseConnectionsHigh
expr: pg_stat_database_numbackends / max_connections > 0.8
for: 5m
```
Why: Detect before hitting limit

**Prevention Measures**:
• Add config validation (app pool < DB max_connections)
• Set query timeout (prevent runaway queries)
• Monitor connection pool usage
• Add capacity alert at 80%

**Implementation Estimate**:
• Config fix: 5 minutes
• Query optimization: 1-2 hours (depends on query complexity)
• Monitoring setup: 30 minutes

Want me to help with implementation details?"

Turn 6:
User: "Thanks! Fixes applied and working. Can you generate the case report and runbook?"

┌─────────────────────────────────────┐
│ Phase Transition:                    │
│ • Phase: SOLUTION (5) → DOCUMENTATION (6)│
│ • Solution implemented: YES          │
│ • Ready for artifacts: YES           │
└─────────────────────────────────────┘

Agent Response:
"🎉 **Solution Implemented Successfully!**

Generating artifacts...

📋 **Case Report Generated**

**Database Outage Post-Mortem - October 7, 2025**

**Incident Summary**:
• Duration: 84 minutes (14:23 - 15:47 UTC)
• Impact: All database-dependent services unavailable
• Root Cause: Connection pool exhaustion due to config mismatch + slow queries

**Timeline**:
• 12:23 UTC: Slow query (5 min) begins executing regularly
• 12:23-14:23: Connections accumulate (20 → 100)
• 14:23 UTC: max_connections (100) reached
• 14:23-15:47: Service degraded (connection timeouts)
• 15:47 UTC: Service restored (manual intervention)

**Root Cause** (Confidence: 0.85):
1. Configuration mismatch: App pool=200, DB max_connections=100
2. Slow query (5 min) held connections open longer than expected
3. Gradual accumulation hit hard limit
4. Cascading connection rejections

**Solution Implemented**:
• Increased max_connections to 250
• Optimized slow query (added index)
• Added connection pool monitoring (alert at 80%)
• Added query timeout (30s)

**Lessons Learned**:
• Validate app pool < DB connections in config validation
• Monitor connection pool usage proactively
• Query timeouts prevent resource exhaustion

[Download Full Report] [View in KB]

---

📖 **Runbook Generated**

**Runbook: Database Connection Pool Exhaustion**

**Detection**:
Symptoms:
• "Connection timeout" errors in application logs
• Database connections at max_connections limit
• Gradual service degradation (not sudden failure)

Monitoring:
• Alert: DatabaseConnectionsHigh (> 80% of max)
• Dashboard: DB Connections > Connections over time

**Quick Diagnosis**:
```bash
# 1. Check current connection count
psql -c "SELECT count(*) FROM pg_stat_activity;"

# 2. Check max_connections limit
psql -c "SHOW max_connections;"

# 3. Identify long-running queries
psql -c "SELECT pid, now() - query_start as duration, query
         FROM pg_stat_activity
         WHERE state = 'active'
         ORDER BY duration DESC LIMIT 10;"
```

**Resolution** (Mitigation + Permanent Fix):

**Immediate Mitigation** (if connections at limit):
```bash
# Kill long-running queries (CAUTION: verify before killing)
psql -c "SELECT pg_terminate_backend(pid)
         FROM pg_stat_activity
         WHERE state = 'active'
         AND now() - query_start > interval '5 minutes';"
```

**Permanent Fix**:
1. Increase max_connections (if app pool > DB limit)
2. Optimize slow queries (add indexes, tune queries)
3. Add query timeout (prevent runaway queries)
4. Monitor connection pool usage (alert before limit)

**Prevention**:
• Config validation: Ensure app_pool_size < db_max_connections - 10
• Query timeout: Set statement_timeout = '30s'
• Monitoring: Alert at 80% connection usage
• Capacity planning: Review connection limits quarterly

**Escalation**:
• If mitigation fails: Contact Database Team (#db-team)
• If queries can't be killed: Database restart required (Platform Lead approval)

[Download Runbook] [Add to KB]

---

Would you like me to:
• Add these to the knowledge base (for future reference)
• Create follow-up tasks (implement prevention measures)
• Close this case

What would you like to do?"
```

**Key Characteristics**:
- **Starts at Phase 4** (skip Phases 1-3, incident already resolved)
- **Thorough evidence collection** (not rushed)
- **High confidence required** (≥0.75 for POST_MORTEM)
- **Comprehensive artifacts** (case report + runbook)
- **Knowledge capture** for organizational learning

---

## API Contracts & Event Flows

### Frontend → Backend API Flow

#### 1. Query Processing with Mode Detection

**Endpoint**: `POST /api/v1/agent/cases/{case_id}/query`

**Request**:
```json
{
  "query": "Our payment API is completely down!",
  "session_id": "session-uuid",
  "context": {
    "urgency_hint": "high",
    "user_id": "user-123"
  }
}
```

**Backend Processing Flow**:

```
1. Receive Query
   ↓
2. Load Case State (CaseDiagnosticState with conversational_state)
   ↓
3. Classify Query Intent
   ├─ Extract incident signals
   ├─ Calculate confidence
   └─ Determine if mode transition needed
   ↓
4. Check Current Mode
   ├─ If CONSULTANT && signals detected && confidence ≥ 0.7
   │  └─ Generate permission request
   │
   └─ If LEAD_INVESTIGATOR
      └─ Process within current phase
   ↓
5. Generate Response (via AgentService)
   ├─ Answer query
   ├─ Request evidence (if LEAD_INVESTIGATOR)
   ├─ Propose phase transition (if objectives met)
   └─ Include mode transition request (if detected)
   ↓
6. Update State
   ├─ Update evidence tracking
   ├─ Record mode transition proposal (if any)
   ├─ Update phase objectives
   └─ Persist to Redis
   ↓
7. Return Response
```

**Response (Mode Transition Requested)**:
```json
{
  "schema_version": "4.0.0",
  "content": "🚨 CRITICAL INCIDENT DETECTED\n\nI understand - payment API down means revenue impact...",
  "response_type": "MODE_TRANSITION_REQUEST",
  "session_id": "session-uuid",
  "case_id": "case-uuid",

  "mode_transition_request": {
    "from_mode": "consultant",
    "to_mode": "lead_investigator",
    "confidence": 0.98,
    "urgency_estimate": "critical",
    "rationale": "User reported critical service outage with business impact",
    "signals_detected": [
      "URGENT keyword",
      "completely down (service down)",
      "can't checkout (business impact)"
    ],
    "awaiting_user_confirmation": true
  },

  "suggested_actions": [
    {
      "label": "YES - START NOW",
      "type": "confirm_mode_transition",
      "payload": "confirm_lead_investigator_mode",
      "metadata": {
        "transition_to": "lead_investigator",
        "entry_phase": "problem_definition"
      }
    },
    {
      "label": "No, I'll handle it",
      "type": "decline_mode_transition",
      "payload": "decline_mode_transition"
    }
  ],

  "conversational_state": {
    "current_mode": "consultant",
    "current_phase": "inquiry",
    "pending_mode_transition": "lead_investigator",
    "awaiting_mode_permission": true
  }
}
```

#### 2. Mode Transition Confirmation

**Endpoint**: `POST /api/v1/agent/cases/{case_id}/confirm-mode-transition`

**Request**:
```json
{
  "confirmed": true,
  "transition_to": "lead_investigator",
  "session_id": "session-uuid"
}
```

**Backend Processing**:
```
1. Validate Pending Transition
   ├─ Check pending_mode_transition exists
   └─ Verify awaiting_mode_permission == true
   ↓
2. Execute Transition
   ├─ Update conversational_mode
   ├─ Set lifecycle_phase (based on transition type)
   ├─ Record transition in history
   └─ Determine investigation_mode (ACTIVE_INCIDENT vs POST_MORTEM)
   ↓
3. Generate First Response in New Mode
   ├─ Set phase objectives
   ├─ Generate evidence requests (if LEAD_INVESTIGATOR)
   └─ Explain what comes next
   ↓
4. Persist State
   ↓
5. Return Response
```

**Response**:
```json
{
  "schema_version": "4.0.0",
  "content": "⚡ IMMEDIATE ACTION MODE\n\n**Problem**: Payment API down...",
  "response_type": "PHASE_ENTRY",
  "session_id": "session-uuid",
  "case_id": "case-uuid",

  "conversational_state": {
    "current_mode": "lead_investigator",
    "current_phase": "problem_definition",
    "investigation_mode": "active_incident",
    "urgency_level": "critical"
  },

  "phase_info": {
    "phase_number": 1,
    "phase_name": "Problem Definition & Impact Analysis",
    "objectives": [
      "Confirm problem statement",
      "Define blast radius",
      "Set urgency level"
    ],
    "methodology": "fast_iteration"
  },

  "evidence_requests": [
    {
      "request_id": "er-001",
      "label": "Pod health check",
      "description": "Check if API pods are running or crashing",
      "category": "symptoms",
      "guidance": {
        "commands": [
          "kubectl get pods -n production -l app=payment-api"
        ],
        "expected_output": "Pod status (Running/CrashLoopBackOff)"
      },
      "status": "pending",
      "created_at_turn": 2
    }
  ],

  "suggested_actions": []
}
```

#### 3. Evidence Submission

**Endpoint**: `POST /api/v1/data/cases/{case_id}/upload` (for files)
**Or**: Regular query endpoint with evidence in text

**Request (File Upload)**:
```json
{
  "file": "<binary data>",
  "filename": "api-logs.txt",
  "content_type": "text/plain",
  "context": {
    "addresses_request_ids": ["er-001"],
    "user_description": "These are the API logs from the crash"
  }
}
```

**Backend Processing**:
```
1. Receive Evidence
   ↓
2. Extract & Summarize Content
   ├─ Parse file/text
   └─ Extract key findings
   ↓
3. Classify Evidence
   ├─ Match to active evidence requests
   ├─ Determine completeness
   ├─ Identify evidence type (supportive/refuting/neutral)
   └─ Detect user intent
   ↓
4. Update Evidence Tracking
   ├─ Record evidence_provided
   ├─ Update evidence_request status (pending→partial→complete)
   └─ Update completeness scores
   ↓
5. Check for Conflicts
   ├─ If evidence_type == REFUTING
   │  └─ Generate conflict detection + confirmation request
   └─ Otherwise, continue
   ↓
6. Generate Immediate Analysis
   ├─ Key findings from evidence
   ├─ How it advances investigation
   ├─ Next steps
   └─ Check if phase objectives met
   ↓
7. Check Phase Transition
   ├─ If objectives met
   │  └─ Generate phase transition proposal
   └─ Otherwise, request more evidence
   ↓
8. Return Immediate Feedback
```

**Response**:
```json
{
  "schema_version": "4.0.0",
  "data_id": "data-uuid",
  "filename": "api-logs.txt",
  "immediate_analysis": {
    "matched_requests": ["er-001"],
    "completeness_scores": {
      "er-001": 1.0
    },
    "key_findings": [
      "Pods in CrashLoopBackOff state",
      "Exit code 1 (error)",
      "Logs show 'NullPointerException in PaymentService.processCheckout'"
    ],
    "evidence_type": "supportive",
    "next_steps": "Evidence confirms pods are crashing. Will analyze root cause of NPE."
  },

  "content": "✅ **Evidence Received: Pod Health Check**\n\nKey Findings:\n• Pods in CrashLoopBackOff\n• NPE in PaymentService...",

  "phase_transition_proposal": {
    "current_phase": "problem_definition",
    "proposed_phase": "triage",
    "objectives_completed": [
      "Confirmed: Pods are crashing (CrashLoopBackOff)",
      "Blast radius: All payment API pods affected",
      "Urgency: CRITICAL (service down)"
    ],
    "next_phase_objectives": [
      "Identify when deployment occurred",
      "Compare with previous working version",
      "Quick mitigation (rollback likely)"
    ],
    "rationale": "Problem is clearly defined, ready to proceed with timeline and mitigation",
    "user_confirmation_required": false
  },

  "conversational_state": {
    "current_mode": "lead_investigator",
    "current_phase": "problem_definition",
    "phase_objectives_met": {
      "1": true
    }
  },

  "evidence_requests": [
    {
      "request_id": "er-001",
      "status": "complete",
      "completeness": 1.0
    }
  ]
}
```

#### 4. Phase Transition Proposal

**Automatic (in response)** or **Explicit Endpoint**: `POST /api/v1/agent/cases/{case_id}/advance-phase`

**Request**:
```json
{
  "confirm_transition": true,
  "session_id": "session-uuid"
}
```

**Backend Processing**:
```
1. Validate Phase Transition
   ├─ Check current_phase objectives met
   └─ Verify user confirmation (if required)
   ↓
2. Execute Phase Transition
   ├─ Increment lifecycle_phase
   ├─ Record phase_entry_turn
   ├─ Reset phase-specific state
   └─ Determine methodology for new phase
   ↓
3. Generate Phase Entry Response
   ├─ Explain new phase objectives
   ├─ Generate evidence requests for new phase
   └─ Set expectations
   ↓
4. Persist State
   ↓
5. Return Response
```

### Backend Event Flow Diagram

```
User Query
    ↓
┌─────────────────────────────────────────┐
│  POST /api/v1/agent/cases/{id}/query    │
└───────────────┬─────────────────────────┘
                ↓
    ┌───────────────────────┐
    │  AgentService         │
    │  .process_query()     │
    └───────┬───────────────┘
            │
            ├─→ QueryClassificationEngine
            │   • Detect incident signals
            │   • Calculate confidence
            │   • Determine mode transition need
            │
            ├─→ Load CaseState from Redis
            │   • conversational_state
            │   • diagnostic_state
            │   • evidence_requests/provided
            │
            ├─→ Mode Decision
            │   ├─ CONSULTANT + Signals → Request Transition
            │   └─ LEAD_INVESTIGATOR → Process in Phase
            │
            ├─→ WorkflowEngine
            │   • Determine current phase workflow
            │   • Generate evidence requests
            │   • Check phase objectives
            │
            ├─→ ResponseSynthesizer
            │   • Assemble response
            │   • Include mode transition request
            │   • Include phase transition proposal
            │
            └─→ Persist State to Redis
                • Update conversational_state
                • Record evidence tracking
                • Save phase progress
                ↓
        ┌──────────────────┐
        │ AgentResponse    │
        │ • content        │
        │ • mode_request   │
        │ • phase_proposal │
        │ • evidence_reqs  │
        └──────────────────┘
```

### State Persistence Schema

**Redis Key Structure**:
```
case:{case_id}:state                  → CaseDiagnosticState (full state)
case:{case_id}:conversational_state   → CaseConversationalState
case:{case_id}:evidence_requests      → List[EvidenceRequest]
case:{case_id}:evidence_provided      → List[EvidenceProvided]
case:{case_id}:messages               → List[CaseMessage]
```

**State Update Event**:
```python
class StateUpdateEvent(BaseModel):
    """Event emitted when case state changes"""

    case_id: str
    session_id: str
    turn_number: int
    timestamp: datetime

    update_type: str = Field(
        ...,
        description="Type of update: mode_transition, phase_transition, evidence_update, etc"
    )

    previous_state: Dict[str, Any]
    new_state: Dict[str, Any]

    trigger: str = Field(..., description="What triggered the update")
```

---

## Implementation Guidance

### Phase 1: Core State Machine (Weeks 1-2)

**Objective**: Implement conversational mode switching and lifecycle phase tracking

**Tasks**:
1. **Data Model Implementation**
   - Add `CaseConversationalState` to `models/case.py`
   - Add `IncidentSignalDetection` to `models/evidence.py`
   - Add `PhaseTransitionProposal` to `models/doctor_patient.py`
   - Extend `CaseDiagnosticState` with conversational fields

2. **Signal Detection Service**
   - Create `services/conversational/signal_detector.py`
   - Implement incident signal detection algorithm
   - Configure confidence thresholds

3. **Mode Transition Logic**
   - Extend `AgentService` with mode transition handling
   - Implement permission request generation
   - Implement confirmation processing

4. **State Persistence**
   - Extend Redis storage for conversational state
   - Implement state update event logging

**Testing**:
- Unit tests for signal detection (30+ test cases)
- Integration tests for mode transitions
- State persistence validation

**Success Criteria**:
- Agent detects incident signals with ≥90% accuracy
- Mode transitions work with explicit user confirmation
- State persists correctly across sessions

---

### Phase 2: Lifecycle Phase Management (Weeks 3-4)

**Objective**: Implement phase progression and objective tracking

**Tasks**:
1. **Phase Objective Definitions**
   - Define objectives for each of 7 phases
   - Create `services/conversational/phase_manager.py`
   - Implement objective completion detection

2. **Phase Transition System**
   - Implement phase transition proposal generation
   - Add user confirmation handling
   - Support phase skipping with warnings

3. **Methodology Adaptation**
   - Implement ACTIVE_INCIDENT methodology (fast iteration)
   - Implement POST_MORTEM methodology (thorough analysis)
   - Dynamic evidence request generation per mode

4. **Entry Point Detection**
   - Implement logic to start at appropriate phase (Phase 4 for post-incident)
   - Support mid-investigation case creation

**Testing**:
- Phase transition scenarios (all 7 phases)
- Objective completion detection
- Methodology adaptation validation

**Success Criteria**:
- Phases advance when objectives are met
- User can skip phases with acknowledgment
- Methodology adapts correctly to urgency

---

### Phase 3: Evidence-Centric Integration (Weeks 5-6)

**Objective**: Integrate lifecycle with existing evidence-centric system

**Tasks**:
1. **Evidence Request Mapping**
   - Map phase objectives to evidence categories
   - Implement phase-aware evidence request generation
   - Lifecycle-driven evidence prioritization

2. **Evidence Tracking Enhancement**
   - Link evidence to phase objectives
   - Implement objective completion based on evidence
   - Phase transition triggers from evidence completeness

3. **Conflict Resolution**
   - Integrate refuting evidence detection
   - Implement user confirmation for contradictions
   - Hypothesis revision workflow

4. **Stall Detection**
   - Implement phase-aware stall detection
   - Disengagement detection
   - Graceful mode reversal

**Testing**:
- Evidence-driven phase transitions
- Conflict resolution scenarios
- Stall detection and recovery

**Success Criteria**:
- Evidence requests align with phase objectives
- Phase transitions trigger from evidence completeness
- Conflicts handled with user confirmation

---

### Phase 4: User Scenario Workflows (Weeks 7-8)

**Objective**: Implement end-to-end scenario flows

**Tasks**:
1. **Scenario Templates**
   - Implement Q&A workflow (Scenario 1)
   - Implement Investigation workflow (Scenario 2)
   - Implement Crisis Response workflow (Scenario 3)
   - Implement Post-Incident RCA workflow (Scenario 4)

2. **Artifact Generation**
   - Implement case report generation (Phase 6)
   - Implement runbook generation (Phase 6)
   - Knowledge base integration

3. **Frontend Integration**
   - Update UI for mode transition requests
   - Add phase progression visualizations
   - Implement suggested actions for mode/phase changes

4. **Analytics & Monitoring**
   - Track mode transition success rates
   - Monitor phase completion times
   - User satisfaction per scenario type

**Testing**:
- End-to-end scenario walkthroughs
- Artifact generation validation
- Frontend integration tests

**Success Criteria**:
- All 4 scenarios work end-to-end
- Artifacts generated correctly
- User can pause/resume investigations

---

### Phase 5: Edge Cases & Polish (Weeks 9-10)

**Objective**: Handle edge cases and optimize UX

**Tasks**:
1. **Edge Case Handling**
   - Mid-conversation mode switches
   - Multiple concurrent issues
   - User overriding agent suggestions
   - Investigation resumption after pause

2. **UX Refinements**
   - Natural language in transitions
   - Clear phase explanations
   - Progress indicators
   - Contextual help

3. **Performance Optimization**
   - Signal detection latency
   - State persistence optimization
   - Redis caching strategy

4. **Documentation**
   - User-facing lifecycle guide
   - Developer implementation docs
   - API documentation updates

**Testing**:
- Edge case scenarios (20+ test cases)
- Performance benchmarks
- User acceptance testing

**Success Criteria**:
- All edge cases handled gracefully
- Signal detection <200ms latency
- User satisfaction ≥4.5/5

---

## Edge Cases & Error Handling

### Edge Case 1: User Switches Topic Mid-Investigation

**Scenario**: User in Phase 4 (Root Cause) suddenly asks unrelated question

**User**: "By the way, what's the difference between Docker and Kubernetes?"

**Detection**:
```python
# QueryClassificationEngine detects off-topic intent
classification = {
    "user_intent": UserIntent.OFF_TOPIC,
    "topic_relevance": 0.1,  # Low relevance to current investigation
    "conversational_mode": ConversationalMode.LEAD_INVESTIGATOR,
    "current_phase": LifecyclePhase.ROOT_CAUSE
}
```

**Agent Response Strategy**:
1. **Answer the question** (respect user autonomy)
2. **Acknowledge context switch**
3. **Offer to resume investigation**

**Response**:
```
Docker and Kubernetes serve different purposes:

**Docker**: Containerization platform
• Packages apps with dependencies
• Runs containers on single host
• Good for development and simple deployments

**Kubernetes**: Container orchestration
• Manages containers across many hosts
• Auto-scaling, load balancing, self-healing
• Production-grade distributed systems

---

We're currently investigating the database connection issue (Phase 4: Root Cause Analysis).

Want to:
• Continue RCA (we were testing the slow query hypothesis)
• Pause investigation and explore more?

[Continue RCA] [Keep Exploring]
```

**State Updates**:
```python
conversational_state.off_topic_turns += 1

# If off_topic_turns >= 2, trigger disengagement detection
if conversational_state.off_topic_turns >= 2:
    agent_suggests_pause_or_mode_switch()
```

---

### Edge Case 2: Multiple Concurrent Issues

**Scenario**: User reports second issue while first investigation ongoing

**User**: "Also, our monitoring dashboard is down. Should we handle that first?"

**Detection**:
```python
# Signal detector identifies new incident
new_incident = {
    "confidence": 0.85,
    "signals": ["monitoring dashboard is down", "should we handle first"],
    "urgency": UrgencyLevel.HIGH,
    "is_new_issue": True  # Distinct from current investigation
}
```

**Agent Response Strategy**:
1. **Acknowledge new issue**
2. **Assess relative priority**
3. **Offer options** (switch, parallel, sequential)

**Response**:
```
🚨 New incident detected: Monitoring dashboard down

**Current investigation**: Database connection exhaustion (Phase 4: Root Cause)
**New issue**: Monitoring dashboard outage

**Priority Assessment**:
• Database: Mitigated (service restored), doing RCA
• Monitoring: Active outage, affects visibility

**Options**:

1️⃣ **Pause current RCA, handle monitoring first** (Recommended)
   • Monitoring down is active incident
   • Database RCA can wait (service is stable)
   • We'll resume database investigation after

2️⃣ **Create new case for monitoring, continue this one**
   • Keep investigations separate
   • You handle monitoring, I'll continue this RCA asynchronously

3️⃣ **Finish database RCA first, then monitoring**
   • Only if monitoring is not critical

What's your priority?

[Switch to Monitoring] [Create New Case] [Finish This First]
```

**State Management**:
```python
if user_selects_switch_to_monitoring:
    # Pause current case
    current_case.status = CaseStatus.STALLED
    current_case.metadata["stall_reason"] = "User switched to higher priority incident"
    current_case.metadata["paused_at_phase"] = LifecyclePhase.ROOT_CAUSE

    # Create new case for monitoring issue
    new_case = create_case(
        title="Monitoring Dashboard Outage",
        priority=CasePriority.HIGH,
        parent_case_id=current_case.case_id  # Link for context
    )

    # Start new investigation
    new_case.conversational_state.conversational_mode = ConversationalMode.LEAD_INVESTIGATOR
    new_case.conversational_state.lifecycle_phase = LifecyclePhase.PROBLEM_DEFINITION
```

---

### Edge Case 3: User Overrides Agent Suggestions

**Scenario**: Agent suggests Phase 2 (Timeline), user wants to jump to Phase 4 (Root Cause)

**Agent**: "Ready to establish the timeline?"

**User**: "No, I already know when it started. Let's just find the root cause."

**Detection**:
```python
# Detect user override intent
override_detection = {
    "user_intent": UserIntent.PHASE_OVERRIDE,
    "proposed_phase": LifecyclePhase.TRIAGE,
    "user_requested_phase": LifecyclePhase.ROOT_CAUSE,
    "confidence": 0.9
}
```

**Agent Response Strategy**:
1. **Acknowledge user's preference**
2. **Warn about potential gaps**
3. **Proceed with user's choice**

**Response**:
```
Understood - you want to skip timeline and jump to root cause analysis.

⚠️ **Heads up**: Skipping timeline means we won't have:
• When the issue started (correlation with changes)
• Recent deployments/configs that may be related
• Pattern identification (intermittent vs constant)

This may reduce confidence in root cause conclusion.

Want to proceed with RCA anyway?

[Yes - Skip Timeline] [Quick Timeline Check First]
```

**State Updates**:
```python
if user_confirms_skip:
    # Record skip in metadata
    diagnostic_state.metadata["skipped_phases"] = [LifecyclePhase.TRIAGE]
    diagnostic_state.metadata["skip_reason"] = "User already knows timeline"

    # Advance to requested phase
    conversational_state.lifecycle_phase = LifecyclePhase.ROOT_CAUSE

    # Adjust confidence requirements (may need lower threshold)
    diagnostic_state.overall_confidence_threshold = 0.70  # Lower from 0.75
```

---

### Edge Case 4: Investigation Resumption After Long Pause

**Scenario**: User returns to case after 24+ hours, needs context refresh

**User**: "Hey, can we continue the database investigation?"

**Detection**:
```python
# Detect case resumption
resumption = {
    "last_activity_hours": 26,
    "conversational_mode": ConversationalMode.LEAD_INVESTIGATOR,
    "lifecycle_phase": LifecyclePhase.ROOT_CAUSE,
    "evidence_requests_pending": 2,
    "context_refresh_needed": True
}
```

**Agent Response Strategy**:
1. **Welcome back**
2. **Provide context summary**
3. **Show progress so far**
4. **Offer to resume or restart**

**Response**:
```
Welcome back! Let's resume the database connection investigation.

📋 **Quick Recap** (Where we left off 26 hours ago):

**Problem**: Database connection exhaustion
**Status**: Service mitigated (max_connections increased to 250)
**Progress**: Phase 4 - Root Cause Analysis
**Current Hypothesis**: Slow queries holding connections open

**What we've learned**:
✓ Connections grew 20 → 100 over 2 hours
✓ App pool (200) > DB limit (100) - mismatch found
✓ 5-minute slow query identified
✗ Exact slow query not yet analyzed
✗ Why query became slow - unknown

**Next Steps**:
• Identify the specific slow query
• Analyze why it's slow (missing index, data growth, etc)
• Design permanent fix

**Pending Evidence Requests** (2):
1. Slow query identification (SQL EXPLAIN ANALYZE)
2. Query performance history

Want to continue where we left off?

[Continue Investigation] [Start Fresh RCA] [Just Ask Questions]
```

**State Restoration**:
```python
# Restore context
case = load_case(case_id)
conversational_state = case.diagnostic_state.conversational_state

# Check if state is still valid (evidence may have expired)
validate_evidence_freshness(case.diagnostic_state.evidence_provided)

# Refresh knowledge base (new runbooks may exist)
check_for_new_knowledge(case.context.problem_description)

# Resume at last phase
agent_continues_from_phase(conversational_state.lifecycle_phase)
```

---

### Edge Case 5: Agent's Hypothesis Completely Wrong

**Scenario**: Agent proposes wrong hypothesis, evidence strongly refutes it

**Agent**: "Based on evidence, root cause is likely memory leak in caching layer."

**User**: "No, I checked - cache memory usage is normal (30%). Logs show disk I/O errors."

**Detection**:
```python
# Evidence classification detects strong refutation
evidence_classification = {
    "evidence_type": EvidenceType.REFUTING,
    "confidence": 0.95,
    "refutes_hypothesis": "memory_leak_in_cache",
    "new_direction": "disk_io_issues",
    "requires_confirmation": True
}
```

**Agent Response Strategy**:
1. **Acknowledge refutation**
2. **Thank user for correction**
3. **Revise hypothesis immediately**
4. **Adjust confidence**

**Response**:
```
⚠️ **Hypothesis Refuted**

You're right - cache memory at 30% rules out memory leak.
Thank you for the correction!

**Previous (Wrong) Hypothesis**: Memory leak in caching layer
**Refuting Evidence**: Cache memory normal, disk I/O errors in logs

**Revised Hypothesis**: Disk I/O bottleneck or failure

This changes our investigation direction:

**New Focus**:
• Disk health (SMART status, error rates)
• Disk I/O latency (iostat, iotop)
• Filesystem issues (full disk, inode exhaustion)

**Confidence Update**: 0.75 → 0.50 (restarting hypothesis validation)

Evidence needed:

1️⃣ **Disk health status**
   ```bash
   sudo smartctl -a /dev/sda
   df -h  # Check disk usage
   ```

2️⃣ **I/O statistics**
   ```bash
   iostat -x 1 10  # 10 seconds of I/O stats
   ```

3️⃣ **Error logs**
   ```bash
   dmesg | grep -i 'error\|fail'
   journalctl -u postgresql | grep 'I/O'
   ```

Let's test this new direction. What do the disk health checks show?
```

**State Updates**:
```python
# Record dead-end hypothesis
dead_end = DeadEnd(
    hypothesis="Memory leak in caching layer",
    evidence_checked=["cache_memory_usage"],
    why_ruled_out="Cache memory at 30%, within normal range",
    turn_number=current_turn,
    confidence_eliminated=0.95
)
diagnostic_state.investigation_dead_ends.append(dead_end)

# Update current hypothesis
diagnostic_state.hypotheses = [
    {
        "hypothesis": "Disk I/O bottleneck or disk failure",
        "confidence": 0.50,
        "supporting_evidence": ["disk I/O errors in logs"],
        "status": "testing"
    }
]

# Lower overall confidence (incorrect hypothesis path)
diagnostic_state.overall_confidence_score = 0.50
```

---

### Error Handling Patterns

#### Timeout Handling

```python
class TimeoutError(Exception):
    """LLM request timeout"""
    pass

async def handle_llm_timeout():
    """When LLM provider times out"""

    return AgentResponse(
        content=(
            "⏱️ Request timed out. This usually means the LLM is overloaded.\n\n"
            "Your investigation is saved. You can:\n"
            "• Try again in a moment\n"
            "• Continue where you left off\n"
            "• Ask a simpler question"
        ),
        response_type=ResponseType.ERROR,
        metadata={
            "error_type": "timeout",
            "recovery_options": ["retry", "resume", "simplify"]
        }
    )
```

#### Invalid State Transitions

```python
def validate_phase_transition(current_phase, proposed_phase):
    """Validate phase transition is legal"""

    # Can't go backward
    if proposed_phase < current_phase:
        raise InvalidTransitionError(
            f"Cannot move backward from {current_phase} to {proposed_phase}"
        )

    # Can't skip more than 1 phase without user override
    if proposed_phase > current_phase + 1:
        requires_user_confirmation = True

    # Phase 3 can be skipped in POST_MORTEM mode
    if current_phase == LifecyclePhase.TRIAGE and \
       proposed_phase == LifecyclePhase.ROOT_CAUSE and \
       investigation_mode == InvestigationMode.POST_MORTEM:
        return True  # Valid skip

    return True
```

#### Data Corruption Recovery

```python
async def recover_from_corrupted_state(case_id):
    """Recover when case state is corrupted"""

    # Load from backup
    backup_state = load_backup_state(case_id)

    if backup_state:
        # Restore from most recent valid backup
        restore_state(case_id, backup_state)
        return "State restored from backup"

    # Fallback: Reset to safe defaults
    safe_state = CaseDiagnosticState(
        conversational_state=CaseConversationalState(
            conversational_mode=ConversationalMode.CONSULTANT,
            lifecycle_phase=LifecyclePhase.INQUIRY
        )
    )

    restore_state(case_id, safe_state)
    return "State reset to defaults - investigation can continue from scratch"
```

---

## Success Metrics

### User Experience Metrics

1. **Mode Transition Acceptance Rate**
   - **Definition**: % of mode transition requests accepted by users
   - **Target**: ≥ 85%
   - **Measurement**: `accepted_transitions / total_transition_requests`

2. **Phase Completion Rate**
   - **Definition**: % of investigations that reach Phase 6 (Documentation)
   - **Target**: ≥ 70% for Lead Investigator mode
   - **Measurement**: `cases_reached_phase_6 / cases_entered_lead_investigator`

3. **Investigation Resumption Success**
   - **Definition**: % of paused investigations successfully resumed
   - **Target**: ≥ 90%
   - **Measurement**: `resumed_and_completed / total_paused`

4. **Time to Resolution by Scenario**
   - **Scenario 2 (Investigation)**: Target < 2 hours average
   - **Scenario 3 (Crisis)**: Target < 30 minutes to mitigation
   - **Scenario 4 (Post-Incident)**: Target < 4 hours average

5. **User Satisfaction by Mode**
   - **Consultant Mode**: Target ≥ 4.5/5
   - **Lead Investigator Mode**: Target ≥ 4.3/5
   - **Measurement**: Post-interaction survey

### System Performance Metrics

1. **Signal Detection Accuracy**
   - **Definition**: Correct identification of incident signals
   - **Target**: ≥ 90% precision, ≥ 85% recall
   - **Measurement**: Manual review of 100+ samples per week

2. **Phase Transition Timing**
   - **Definition**: Agent proposes transition at right time (not too early/late)
   - **Target**: ≥ 85% appropriate timing
   - **Measurement**: User feedback + objective metrics (evidence completeness)

3. **Evidence Request Relevance**
   - **Definition**: Evidence requests are relevant to current phase
   - **Target**: ≥ 90% user-rated as relevant
   - **Measurement**: User ratings + completion rate

4. **State Persistence Reliability**
   - **Definition**: No state loss or corruption
   - **Target**: 99.9% reliability
   - **Measurement**: Error logs + state validation checks

5. **Response Latency**
   - **Signal Detection**: < 200ms
   - **Mode Transition Request**: < 500ms
   - **Evidence Classification**: < 300ms
   - **Phase Transition Proposal**: < 400ms

### Business Impact Metrics

1. **Incident Resolution Time Reduction**
   - **Definition**: Time to resolution vs baseline (no structured approach)
   - **Target**: 30% reduction in median resolution time
   - **Measurement**: Compare cases with/without Lead Investigator mode

2. **Knowledge Capture Rate**
   - **Definition**: % of resolved cases with generated runbooks
   - **Target**: ≥ 60% for new issue types
   - **Measurement**: `runbooks_generated / resolved_cases`

3. **Runbook Reuse Rate**
   - **Definition**: % of subsequent incidents using generated runbooks
   - **Target**: ≥ 40% within 30 days
   - **Measurement**: Track KB document access patterns

4. **Escalation Reduction**
   - **Definition**: % of incidents resolved without human escalation
   - **Target**: 20% increase vs baseline
   - **Measurement**: `resolved_without_escalation / total_incidents`

---

## Appendix A: State Machine Formal Specification

### Mode State Machine

```
States:
  S0: CONSULTANT_MODE
  S1: LEAD_INVESTIGATOR_MODE

Transitions:
  T1: S0 → S0 (default, user asks questions)
  T2: S0 → S1 (incident signal detected, confidence ≥ 0.7, user confirms)
  T3: S1 → S1 (investigation progressing)
  T4: S1 → S0 (user requests exit OR disengagement detected)

Events:
  E1: user_query_received
  E2: incident_signal_detected(confidence)
  E3: user_confirms_mode_transition
  E4: user_declines_mode_transition
  E5: disengagement_detected
  E6: user_requests_exit

Guards:
  G1: confidence ≥ 0.7
  G2: awaiting_mode_permission == False
  G3: evidence_response_rate < 0.3 AND turns_without_progress ≥ 3

Actions:
  A1: generate_mode_transition_request()
  A2: execute_mode_transition()
  A3: record_mode_transition()
  A4: reset_investigation_state()
```

### Phase State Machine

```
States:
  P0: INQUIRY (Consultant Mode only)
  P1: PROBLEM_DEFINITION
  P2: TRIAGE
  P3: MITIGATION (ACTIVE_INCIDENT only)
  P4: ROOT_CAUSE
  P5: SOLUTION
  P6: DOCUMENTATION
  PEND: CLOSED

Transitions:
  T1: P0 → P1 (mode transition to LEAD_INVESTIGATOR)
  T2: P1 → P2 (problem confirmed, blast radius defined)
  T3: P2 → P3 (timeline complete, investigation_mode == ACTIVE_INCIDENT)
  T4: P2 → P4 (timeline complete, investigation_mode == POST_MORTEM)
  T5: P3 → P4 (service mitigated, transition to POST_MORTEM)
  T6: P4 → P5 (root cause identified, confidence ≥ threshold)
  T7: P5 → P6 (solution implemented)
  T8: P6 → PEND (artifacts handled)
  T9: any → P0 (user exits LEAD_INVESTIGATOR mode)

Events:
  E1: mode_transition_confirmed
  E2: phase_objectives_met(phase)
  E3: user_confirms_phase_transition
  E4: service_restored
  E5: root_cause_found(confidence)
  E6: solution_validated
  E7: artifacts_generated

Guards:
  G1: phase_objectives[phase] == True
  G2: investigation_mode == ACTIVE_INCIDENT
  G3: investigation_mode == POST_MORTEM
  G4: confidence ≥ confidence_threshold
  G5: user_confirmation == True (if required)

Actions:
  A1: set_lifecycle_phase(new_phase)
  A2: generate_phase_entry_response()
  A3: generate_evidence_requests(phase)
  A4: record_phase_transition()
  A5: update_methodology(investigation_mode)
```

---

## Appendix B: Response Type Specifications

### Response Type Enum

```python
class ResponseType(str, Enum):
    """Extended response types for lifecycle management"""

    # Existing types
    ANSWER = "answer"
    NEEDS_MORE_DATA = "needs_more_data"
    CLARIFICATION_REQUEST = "clarification_request"
    TOOL_EXECUTION = "tool_execution"
    ERROR = "error"

    # NEW: Conversational mode types
    MODE_TRANSITION_REQUEST = "mode_transition_request"
    MODE_TRANSITION_CONFIRMED = "mode_transition_confirmed"

    # NEW: Phase progression types
    PHASE_ENTRY = "phase_entry"
    PHASE_TRANSITION_PROPOSAL = "phase_transition_proposal"
    PHASE_OBJECTIVE_UPDATE = "phase_objective_update"

    # NEW: Investigation types
    EVIDENCE_ANALYSIS = "evidence_analysis"
    HYPOTHESIS_PROPOSAL = "hypothesis_proposal"
    CONFLICT_DETECTION = "conflict_detection"
    MITIGATION_GUIDANCE = "mitigation_guidance"

    # NEW: Closure types
    RESOLUTION_SUMMARY = "resolution_summary"
    ARTIFACT_OFFER = "artifact_offer"
```

---

## Appendix C: Prompt Engineering Guidelines

### Mode-Specific System Prompts

#### Consultant Mode Prompt

```
You are FaultMaven, an expert SRE consultant.

MODE: Consultant Mode
BEHAVIOR: Reactive, supportive, educational

Your role:
• Answer user questions directly and thoroughly
• Provide examples and best practices
• Offer suggested learning paths
• Do NOT push structured troubleshooting unless user has a problem

Guidelines:
• Keep tone conversational and friendly
• Provide code examples when relevant
• Suggest related topics for exploration
• If you detect a problem, SUGGEST (don't insist) structured troubleshooting

DO NOT:
• Generate evidence requests (only in Lead Investigator mode)
• Track investigation phases (only in Lead Investigator mode)
• Push users into troubleshooting mode

DETECTION:
If user mentions a problem (errors, issues, incidents), analyze signals:
• Calculate confidence (0-1)
• If confidence ≥ 0.7, generate mode transition request
• If confidence 0.4-0.69, mention possibility
• If confidence < 0.4, continue answering

Example:
User: "What's the difference between sync and async in Python?"
You: [Explain directly, offer learning paths, stay in Consultant mode]

User: "Our API is timing out for some users"
You: [Detect signal, confidence 0.8, request transition to Lead Investigator mode]
```

#### Lead Investigator Mode Prompt (ACTIVE_INCIDENT)

```
You are FaultMaven, an expert SRE investigator.

MODE: Lead Investigator (ACTIVE_INCIDENT)
URGENCY: CRITICAL
METHODOLOGY: Fast iteration, mitigation first

Your role:
• Guide user through rapid incident response
• Generate evidence requests with commands
• Prioritize service restoration over certainty
• Drive toward mitigation quickly

Current Phase: {current_phase}
Phase Objectives:
{phase_objectives}

Guidelines:
• ONE evidence request at a time (speed over completeness)
• Commands should be copy-paste ready
• Explain WHY evidence is needed (builds trust)
• Propose quick mitigation when identified
• Skip phases if timeline is obvious

Evidence Format:
🔴 **IMMEDIATE ACTION**

Run NOW:
```bash
[command]
```

Expected: [what to look for]

After service restored:
• Transition to POST_MORTEM mode
• Continue with thorough RCA

DO NOT:
• Request comprehensive evidence (too slow)
• Require high confidence (mitigation > certainty)
• Skip mitigation phase

Example:
User: "Payment API completely down!"
You: [Request pod health check, suggest rollback if recent deployment]
```

#### Lead Investigator Mode Prompt (POST_MORTEM)

```
You are FaultMaven, an expert SRE investigator.

MODE: Lead Investigator (POST_MORTEM)
URGENCY: NORMAL
METHODOLOGY: Thorough analysis, high confidence

Your role:
• Guide user through comprehensive RCA
• Generate evidence requests (2-3 per turn)
• Test multiple hypotheses systematically
• Require confidence ≥ 0.75 for root cause conclusions

Current Phase: {current_phase}
Phase Objectives:
{phase_objectives}

Guidelines:
• Comprehensive evidence collection (no rush)
• Multiple evidence requests per turn (2-3)
• Test all viable hypotheses
• Explicit confidence scoring
• Document dead-ends and alternatives

Evidence Format:
Evidence needed for root cause analysis:

1️⃣ **[Category]: [What's needed]**

   Why: [Rationale]

   Commands:
   • [command 1]
   • [command 2]

   Expected: [what to look for]

2️⃣ [Next evidence request]

Root Cause Conclusions:
When proposing root cause, include:
• Confidence score (0-1)
• Supporting evidence list
• Missing evidence list
• Alternative explanations (with why less likely)
• Caveats/assumptions

Confidence Rubric:
0.9-1.0: Direct evidence confirms
0.75-0.89: Strong supporting evidence, minor gaps
0.5-0.74: Circumstantial evidence, notable gaps
< 0.5: Insufficient for conclusion

DO NOT:
• Rush to conclusions (service is stable)
• Accept confidence < 0.75
• Skip alternative hypothesis testing

Example:
User: "Want to understand yesterday's outage"
You: [Start at Phase 4, request comprehensive evidence, test multiple hypotheses]
```

---

## Document Metadata

**Version**: 1.0
**Type**: Design Specification
**Audience**: Engineers implementing conversational interaction model
**Prerequisites**: Understanding of Evidence-Centric Troubleshooting (v3.1.0) and Doctor/Patient Architecture
**Related Documents**:
- `/home/swhouse/projects/FaultMaven/docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md`
- `/home/swhouse/projects/FaultMaven/docs/architecture/DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md`
- `/home/swhouse/projects/FaultMaven/docs/architecture/SUB_AGENT_ARCHITECTURE.md`

**Changelog**:
- 2025-10-08: Initial v1.0 release - Complete lifecycle and mode design

---

**END OF DESIGN SPECIFICATION**
