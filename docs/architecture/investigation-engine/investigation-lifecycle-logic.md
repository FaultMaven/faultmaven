# Investigation Lifecycle Logic

This document defines the state transitions, path routing, and turn tracking logic for FaultMaven's opportunistic investigation framework.

**Related Documents**:
- [Opportunistic Investigation Framework](./opportunistic-investigation-framework.md) - Overview and philosophy
- [Investigation Data Models](./investigation-data-models.md) - Core data structures

---

## Table of Contents

1. [Investigation Lifecycle](#1-investigation-lifecycle)
2. [Path Selection & Routing](#2-path-selection--routing)
3. [Turn Progress Tracking](#3-turn-progress-tracking)

---

## 1. Investigation Lifecycle

### 1.1 Status Transition Map

```
┌──────────────┐
│    INQUIRY   │
│              │
│ Exploring    │
└──────┬───────┘
       │
       ├─────(User decides to investigate)────────┐
       │                                          │
       │                                          ▼
       │                              ┌────────────────────┐
       │                              │   INVESTIGATING    │
       │                              │                    │
       │                              │ Verification       │
       │                              │ Investigation      │
       │                              │ Resolution         │
       │                              └─────────┬──────────┘
       │                                        │
       │                              ┌─────────┴──────────┐
       │                              │                    │
       │                   (solution_verified)    (no solution,
       │                              │            abandoned/escalated)
       │                              │                    │
       │                              ▼                    ▼
       │                      ┌──────────────┐    ┌──────────────┐
       │                      │   RESOLVED   │    │    CLOSED    │
       │                      │              │    │              │
       │                      │ TERMINAL     │    │  TERMINAL    │
       │                      │ With solution│    │ No solution  │
       │                      └──────────────┘    └──────────────┘
       │                                                  ▲
       └──(no investigation needed)──────────────────────┘
          (inquiry-only)
```

### 1.2 Status Transitions

#### INQUIRY → INVESTIGATING

**Trigger**: User commits to formal investigation AND confirms problem statement

```python
def can_start_investigation(case: Case) -> bool:
    """
    Requires:
    1. Problem confirmation (agent understands problem type)
    2. Problem statement formalized and confirmed by user
    3. User decided to investigate
    """
    return (
        case.status == CaseStatus.INQUIRY and
        case.inquiry.problem_confirmation is not None and
        case.inquiry.proposed_problem_statement is not None and
        case.inquiry.problem_statement_confirmed == True and
        case.inquiry.decided_to_investigate == True
    )

if can_start_investigation(case):
    # Create ProblemVerification with confirmed statement
    case.problem_verification = ProblemVerification(
        symptom_statement=case.inquiry.proposed_problem_statement
        # LLM will fill other fields during investigation
    )

    transition_status(case, CaseStatus.INVESTIGATING, "system",
                     "User confirmed problem and decided to investigate")
```

#### INVESTIGATING → RESOLVED (Terminal)

**Trigger**: Solution verified

```python
def can_mark_resolved(case: Case) -> bool:
    return (
        case.status == CaseStatus.INVESTIGATING and
        case.progress.solution_verified == True
    )

if can_mark_resolved(case):
    case.status = CaseStatus.RESOLVED
    case.resolved_at = datetime.now(timezone.utc)
    case.closed_at = datetime.now(timezone.utc)
    case.closure_reason = "resolved"
    # TERMINAL - no further transitions
```

#### INVESTIGATING → CLOSED (Terminal)

**Trigger**: Investigation abandoned without solution

```python
def force_close_investigation(case: Case, user_id: str, reason: str):
    """User abandons investigation without solution"""
    case.status = CaseStatus.CLOSED
    case.closed_at = datetime.now(timezone.utc)
    case.closure_reason = reason  # "abandoned" | "escalated" | "other"
    # TERMINAL - no further transitions
```

#### INQUIRY → CLOSED (Terminal)

**Trigger**: Inquiry-only, no investigation needed

```python
def close_from_inquiry(case: Case, user_id: str):
    """Close after inquiry without formal investigation"""
    case.status = CaseStatus.CLOSED
    case.closed_at = datetime.now(timezone.utc)
    case.closure_reason = "inquiry_only"
    # TERMINAL - no further transitions
```

#### INQUIRY → RESOLVED (Terminal, Fast-Track)

**Trigger**: Knowledge base match resolves issue without formal investigation

This "Fast-Track" path allows instant resolution when the knowledge base contains
a high-confidence match for the user's problem and the user confirms the solution worked.

```python
class KnowledgeResolution(BaseModel):
    """Records instant resolution via knowledge base match."""
    match_id: str                # ID of case/runbook that solved it
    match_type: str              # "past_case" | "runbook" | "documentation"
    solution_applied: str        # What user actually did
    user_confirmation: str       # User's message confirming fix
    resolution_turn: int         # Turn when confirmed

def fast_track_resolution(case: Case, knowledge_resolution: KnowledgeResolution):
    """
    Fast-Track: INQUIRY → RESOLVED (skipping INVESTIGATING)

    Conditions:
    1. Knowledge base search found high-confidence match (>70%)
    2. Agent offered known solution to user
    3. User tried solution and confirmed it worked
    """
    case.status = CaseStatus.RESOLVED
    case.resolved_at = datetime.now(timezone.utc)
    case.closed_at = datetime.now(timezone.utc)
    case.closure_reason = "knowledge_base_resolution"
    case.knowledge_resolution = knowledge_resolution
    # TERMINAL - no further transitions
```

**Flow**:
```
1. INQUIRY: Agent searches KB, finds high-confidence match
2. INQUIRY: Agent says "This looks similar to [past case]. Solution was [X]."
3. INQUIRY: User tries solution, confirms "Yes, that fixed it!"
4. System: Transition directly to RESOLVED (skip INVESTIGATING)
```

**Metrics Implications**:
- `time_to_resolution`: Extremely low (1-2 turns)
- `resolution_type`: "knowledge_base" (vs "investigation")
- `knowledge_attribution`: Which KB item resolved it

This keeps investigation metrics clean while highlighting KB value.

### 1.3 Valid Transitions Summary

```python
VALID_TRANSITIONS = {
    CaseStatus.INQUIRY: [
        CaseStatus.INVESTIGATING,  # Start formal investigation
        CaseStatus.RESOLVED,        # Fast-Track: KB match resolved issue
        CaseStatus.CLOSED           # Inquiry-only, no investigation
    ],
    CaseStatus.INVESTIGATING: [
        CaseStatus.RESOLVED,        # Solution verified (terminal)
        CaseStatus.CLOSED           # Abandoned (terminal)
    ],
    CaseStatus.RESOLVED: [],        # TERMINAL - no transitions
    CaseStatus.CLOSED: []           # TERMINAL - no transitions
}
```

**Transition Diagram** (updated with Fast-Track):

```
┌──────────────┐
│    INQUIRY   │
│              │
│ Exploring    │
└──────┬───────┘
       │
       ├─────(User decides to investigate)───────┐
       │                                         │
       ├─────(KB match + user confirms)──────────┼────────────┐
       │     [FAST-TRACK]                        │            │
       │                                         ▼            │
       │                             ┌────────────────────┐   │
       │                             │   INVESTIGATING    │   │
       │                             │                    │   │
       │                             │ Verification       │   │
       │                             │ Investigation      │   │
       │                             │ Resolution         │   │
       │                             └─────────┬──────────┘   │
       │                                       │              │
       │                             ┌─────────┴──────────┐   │
       │                             │                    │   │
       │                  (solution_verified)   (no solution) │
       │                             │                    │   │
       │                             ▼                    ▼   │
       │                     ┌──────────────┐    ┌──────────────┐
       │                     │   RESOLVED   │◄───┘              │
       │                     │              │                   │
       │                     │ TERMINAL     │    ┌──────────────┐
       │                     │ With solution│    │    CLOSED    │
       │                     └──────────────┘    │              │
       │                                         │  TERMINAL    │
       └──(inquiry-only)─────────────────────────► No solution  │
                                                 └──────────────┘
```

### 1.4 Milestone Progression

**During INVESTIGATING status, milestones complete opportunistically**:

```python
async def process_turn(case: Case, user_message: str):
    """Process one turn and update milestones"""

    # Only process if not terminal
    if case.is_terminal:
        return "Case is closed."

    # Capture state before
    progress_before = case.progress.dict()

    # Agent analyzes available data and completes tasks
    agent_response = await agent.process(case, user_message)

    # Capture state after
    progress_after = case.progress.dict()

    # Detect completed milestones
    milestones_completed = [
        key for key in progress_before
        if isinstance(progress_before[key], bool)
        and progress_before[key] == False
        and progress_after[key] == True
    ]

    # Record turn
    record_turn(case, milestones_completed)

    # Check for automatic status transitions to terminal states
    check_status_transitions(case)

    return agent_response
```

### 1.5 Manual Status Change Requests

**Purpose**: Allow users to manually request status transitions for practical scenarios (urgent issues, external resolutions, etc.)

**Core Principle**: Manual status changes follow the same confirmation pattern as natural progression - **all status changes require explicit user confirmation**.

---

#### 1.5.1 UI Component: Status Dropdown

**Location**: Case header (collapsed view)

**Behavior**:
- Shows current status with dropdown indicator
- Displays only **forward transitions** (status changes are irreversible)
- Terminal states (RESOLVED, CLOSED) have dropdown disabled

**Available Options by Status**:

| Current Status | Dropdown Options |
|---------------|------------------|
| INQUIRY       | Investigating, Closed |
| INVESTIGATING | Resolved, Closed |
| RESOLVED      | *(disabled - terminal state)* |
| CLOSED        | *(disabled - terminal state)* |

**API Support**: No direct API - uses existing query submission endpoint

---

#### 1.5.2 Request Flow

**Step 1: User Initiates Request**

User selects new status from dropdown → Frontend shows confirmation modal:

```
⚠️ Request Status Change

This will ask the agent to transition the case to [NEW_STATUS].

Are you sure you want to proceed?

[Cancel]  [Continue]
```

**API Call**: None yet - just frontend modal

---

**Step 2: Submit Request via Chat**

User confirms modal → Frontend sends system-generated message:

```typescript
POST /api/v1/cases/{case_id}/queries
Body: {
  "message": "[User requested to change case status to Investigating]"
}
```

**API Endpoint**: `POST /api/v1/cases/{case_id}/queries`
- **Purpose**: Submit user messages (including system-generated ones)
- **Auth**: Requires Bearer token + X-Session-Id
- **Returns**: AgentResponse with agent's confirmation message

---

**Step 3: Agent Validates and Asks for Confirmation**

Agent receives request message and responds with:
1. **Context validation** - Ensures prerequisites are met or asks for missing information
2. **Confirmation question** - Presents specific question with Yes/No buttons
3. **Tip text** - Indicates user can type qualified answer

**Example Agent Response** (INQUIRY → INVESTIGATING):

```
"You've requested to move to investigation.

Based on our conversation, the problem is:
'Database queries timing out in production, affecting 30% of requests'

Is this what you want me to investigate?

[✅ Yes]  [❌ No]

💡 Tip: Click a button or type to clarify"
```

**If missing information**, agent asks questions first:

```
"You've requested to move to investigation.

Before we can investigate, I need to understand the problem.
What issue are you experiencing?"

(No buttons yet - waiting for user to provide context)
```

---

**Step 4: User Confirms (3 Options)**

**Option A: Click [✅ Yes]**
- Frontend sends system-generated message: `"Yes"`
- Agent immediately transitions status
- Agent responds with acknowledgment

**Option B: Click [❌ No]**
- Frontend sends system-generated message: `"No"`
- Agent cancels request, stays in current status
- Agent asks what user wants to do next

**Option C: Type qualified answer**
- User types: "Not 30%, more like 50%, and started 3 hours ago"
- Agent refines understanding
- Agent presents confirmation question again with updated context

**API Call for all options**:
```typescript
POST /api/v1/cases/{case_id}/queries
Body: {
  "message": "Yes"  // or "No" or user's typed message
}
```

---

**Step 5: Agent Executes Transition**

If user confirmed (Option A or refined via Option C), agent:

1. **Sets status** to new value
2. **Initializes required state** (e.g., creates `ProblemVerification` for INVESTIGATING)
3. **Records transition** in `status_history`
4. **Responds with acknowledgment** and next steps

**Example response** (INQUIRY → INVESTIGATING):

```
"Understood. Transitioning to formal investigation now.

Based on our discussion, the problem is:
'Database queries timing out in production, affecting 50% of requests
since 3 hours ago'

Let me start by verifying the scope and impact. What services are affected?"
```

**Backend updates**:
- `case.status = CaseStatus.INVESTIGATING`
- `case.problem_verification = ProblemVerification(symptom_statement=...)`
- `case.status_history.append(CaseStatusTransition(...))`

---

#### 1.5.3 Confirmation UI Pattern

**Visual Design** (in chat conversation):

```
┌─────────────────────────────────────────────────┐
│ Agent:                                   2:45 PM│
│                                                 │
│ You've requested to move to investigation.      │
│                                                 │
│ Based on our conversation, the problem is:      │
│ "Database queries timing out in production,     │
│ affecting 30% of requests"                      │
│                                                 │
│ Is this what you want me to investigate?        │
│                                                 │
│ ┌─────────┐  ┌─────────┐                       │
│ │ ✅ Yes  │  │ ❌ No   │                       │
│ └─────────┘  └─────────┘                       │
│                                                 │
│ 💡 Tip: Click a button or type to clarify      │
└─────────────────────────────────────────────────┘
```

**Buttons are rendered by frontend** when agent message contains:
- Confirmation question pattern
- Binary choice indicators

**Button clicks generate system messages**:
- `[✅ Yes]` → Sends `"Yes"` via POST `/queries`
- `[❌ No]` → Sends `"No"` via POST `/queries`

---

#### 1.5.4 Status-Specific Confirmation Examples

**INQUIRY → INVESTIGATING**

```python
# Agent validation
if not case.inquiry.proposed_problem_statement:
    # Missing problem - ask first
    return "Before we can investigate, what problem are we trying to solve?"
else:
    # Present confirmation
    return f"""You've requested to move to investigation.

    The problem is: {case.inquiry.proposed_problem_statement}

    Is this what you want me to investigate?

    [✅ Yes]  [❌ No]"""
```

**INVESTIGATING → RESOLVED**

```python
# Agent asks for resolution details
return f"""You've requested to mark this as resolved.

Problem: {case.problem_verification.symptom_statement}
Root cause: {case.root_cause_conclusion.root_cause if exists else "Not identified"}

What did you do to resolve this issue?

(Agent waits for user to explain, then presents confirmation)
"""
```

**INVESTIGATING → CLOSED**

```python
# Agent confirms closure without resolution
return f"""You've requested to close this case without resolution.

Problem: {case.problem_verification.symptom_statement}
Current findings: {case.working_conclusion.summary if exists else "Limited data"}

Should I close the case and archive our findings?

[✅ Yes]  [❌ No]"""
```

---

#### 1.5.5 API Summary

All manual status changes use **existing endpoints** - no new APIs required:

| Action | Endpoint | Method | Body |
|--------|----------|--------|------|
| Submit status change request | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "[User requested to change case status to Investigating]"}` |
| User clicks Yes button | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "Yes"}` |
| User clicks No button | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "No"}` |
| User types qualified answer | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "<user's typed message>"}` |

**All messages appear in conversation history** - full audit trail maintained.

---

#### 1.5.6 Design Rationale

**Why dropdown menu instead of pure chat?**
- **Discoverability**: Users see available status transitions
- **Clarity**: Visual indicator of current status + forward-only options
- **Efficiency**: One click vs composing message
- **Removes ambiguity**: "Let's investigate" could mean many things

**Why agent confirmation instead of direct status change?**
- **Consistency**: Same pattern as natural progression (all status changes require confirmation)
- **Safety**: Agent can validate prerequisites and catch mistakes
- **Context**: Agent ensures mutual understanding before transition
- **Audit**: Full conversation record of why status changed

**Why buttons + typed fallback?**
- **Efficiency**: Most cases are simple yes/no
- **Flexibility**: User can elaborate when needed
- **Natural**: Matches existing confirmation pattern in natural progression

---

## 2. Path Selection & Routing

### 2.0 Preliminary Urgency Assessment (During INQUIRY)

**Purpose**: Enable faster path hints before formal investigation starts.

During INQUIRY, agent assesses urgency based on **business impact** (semantic), not keywords:

```python
class PreliminaryUrgency(BaseModel):
    """Early urgency signal for faster path selection."""
    level: UrgencyLevel        # CRITICAL | HIGH | MEDIUM | LOW
    is_ongoing: bool           # True if problem appears active NOW
    impact_assessment: str     # Brief description of business impact
    mitigation_hint: Optional[str]  # Quick mitigation if obvious

# Semantic urgency definitions (NOT keyword-based):
URGENCY_DEFINITIONS = {
    "CRITICAL": "Complete service unavailability or data loss/corruption",
    "HIGH": "Significant degradation affecting most users",
    "MEDIUM": "Partial degradation or intermittent issues",
    "LOW": "Minor issues or historical investigation"
}
```

**Why Semantic, Not Keywords**:
- ❌ Keyword-based: "not down" might trigger CRITICAL due to word "down"
- ✅ Semantic: LLM assesses actual business impact from context

**Early Path Hint** (during INQUIRY):
If CRITICAL/HIGH + ONGOING detected, agent offers:
> "This sounds like it's actively impacting users. Should I focus on quick
> mitigation first, then investigate root cause after?"

This accelerates path selection without waiting for full verification.

### 2.1 Path Selection Matrix

Based on **temporal_state × urgency_level**:

| Temporal State | Urgency | Path | Rationale |
|----------------|---------|------|-----------|
| **Ongoing** | CRITICAL | MITIGATION_FIRST (auto) | Production broken NOW - stop impact, RCA later |
| **Ongoing** | HIGH | MITIGATION_FIRST (auto) | Significant active impact - stop bleeding first |
| **Ongoing** | MEDIUM | USER_CHOICE | User decides: quick mitigation or thorough RCA |
| **Ongoing** | LOW | USER_CHOICE | Minor issue, user decides approach |
| **Historical** | CRITICAL | USER_CHOICE | Clarify why critical if past issue |
| **Historical** | HIGH | USER_CHOICE | High urgency for past issue? |
| **Historical** | MEDIUM | ROOT_CAUSE (auto) | Standard post-mortem - find root cause |
| **Historical** | LOW | ROOT_CAUSE (auto) | Thorough investigation - permanent solution |

### 2.2 Path Selection Logic

```python
def determine_investigation_path(
    problem_verification: ProblemVerification
) -> PathSelection:
    """Determine investigation path after verification complete"""

    temporal = problem_verification.temporal_state
    urgency = problem_verification.urgency_level

    # AUTO: Ongoing + High Urgency → MITIGATION_FIRST (then RCA)
    if temporal == TemporalState.ONGOING and urgency in [UrgencyLevel.CRITICAL, UrgencyLevel.HIGH]:
        return PathSelection(
            path=InvestigationPath.MITIGATION_FIRST,
            auto_selected=True,
            rationale=f"Ongoing {urgency.value} issue requires immediate mitigation, RCA after impact stopped",
            alternate_path=InvestigationPath.ROOT_CAUSE
        )

    # AUTO: Historical + Low Urgency → ROOT_CAUSE (permanent solution)
    if temporal == TemporalState.HISTORICAL and urgency in [UrgencyLevel.LOW, UrgencyLevel.MEDIUM]:
        return PathSelection(
            path=InvestigationPath.ROOT_CAUSE,
            auto_selected=True,
            rationale=f"Historical {urgency.value} issue allows thorough investigation with permanent solution",
            alternate_path=InvestigationPath.MITIGATION_FIRST
        )

    # USER CHOICE: Ambiguous cases - let user decide between paths
    return PathSelection(
        path=InvestigationPath.USER_CHOICE,
        auto_selected=False,
        rationale=f"Ambiguous case ({temporal.value} + {urgency.value}): User chooses (a) mitigation first or (b) RCA",
        alternate_path=None
    )
```

### 2.3 Path Impact on Investigation

**Both paths follow LINEAR stage progression: 1 → 2 → 3 → 4**

The difference is WHEN mitigation is applied, not the stage flow.

---

**Path (a): MITIGATION_FIRST**

Mitigation is a **tool available during early stages**, not a stage jump.

- **Stage 1: Symptom Verification**
  - Verify where and when problem is happening
  - Assess urgency and temporal state
  - **If correlation strong** (e.g., error started 2 min after deploy):
    - Apply quick mitigation (rollback, restart, etc.)
    - Mark `mitigation_applied = True`
    - Service stabilized, pressure reduced
  - Next: Stage 2

- **Stage 2: Hypothesis Formulation**
  - Generate theories about root cause
  - Service is now stable, can take time for thorough analysis
  - May use systematic exploration when cause unclear
  - Next: Stage 3

- **Stage 3: Hypothesis Validation**
  - Test hypotheses with diagnostic evidence
  - Identify root cause with confidence
  - Mark `root_cause_identified = True`
  - Next: Stage 4

- **Stage 4: Solution**
  - Apply evidence-based permanent fix
  - Address root cause to prevent recurrence
  - Verify effectiveness
  - Case transitions to RESOLVED

**Milestones**: `symptom_verified` → `mitigation_applied` (during 1-2) → `root_cause_identified` → `solution_applied` → `solution_verified`

---

**Path (b): ROOT_CAUSE**

Traditional RCA path - thorough investigation from start.

- **Stage 1: Symptom Verification**
  - Verify where and when (historical problem or low urgency)
  - No immediate mitigation needed (no active impact)
  - Next: Stage 2

- **Stage 2: Hypothesis Formulation**
  - Generate theories systematically
  - Next: Stage 3

- **Stage 3: Hypothesis Validation**
  - Test hypotheses, identify root cause
  - Mark `root_cause_identified = True`
  - Next: Stage 4

- **Stage 4: Solution**
  - Apply permanent solution based on root cause
  - Verify effectiveness
  - Case transitions to RESOLVED

**Milestones**: `symptom_verified` → `root_cause_identified` → `solution_applied` → `solution_verified`

---

**Key Differences**:

| Aspect | MITIGATION_FIRST | ROOT_CAUSE |
|--------|------------------|------------|
| Stage flow | Linear: 1 → 2 → 3 → 4 | Linear: 1 → 2 → 3 → 4 |
| Mitigation timing | During stages 1-2 (opportunistic) | Not applied (or only if urgent) |
| Pressure | Reduced early (service stable) | Full pressure until resolution |
| Use case | ONGOING + HIGH/CRITICAL | HISTORICAL + LOW/MEDIUM |

---

## 3. Turn Progress Tracking

```python
async def record_turn(
    case: Case,
    user_message: str,
    agent_response: str
) -> TurnProgress:
    """Record turn and detect progress"""

    # Capture state before
    progress_before = case.progress.dict()
    evidence_count_before = len(case.evidence)

    # Process turn (agent work happens here)

    # Capture state after
    progress_after = case.progress.dict()
    evidence_count_after = len(case.evidence)

    # Detect milestones completed
    milestones_completed = [
        key for key in progress_before
        if isinstance(progress_before[key], bool)
        and progress_before[key] == False
        and progress_after[key] == True
    ]

    # Detect evidence added
    evidence_added = []
    if evidence_count_after > evidence_count_before:
        new_evidence = case.evidence[evidence_count_before:]
        evidence_added = [e.evidence_id for e in new_evidence]

    # Determine if progress made
    progress_made = (
        len(milestones_completed) > 0 or
        len(evidence_added) > 0 or
        any(h.status == HypothesisStatus.VALIDATED for h in case.hypotheses.values())
    )

    # Create turn record
    turn = TurnProgress(
        turn_number=case.current_turn,
        milestones_completed=milestones_completed,
        evidence_added=evidence_added,
        progress_made=progress_made,
        outcome=determine_turn_outcome(case, progress_made)
    )

    case.turn_history.append(turn)
    case.current_turn += 1

    # Track turns without progress
    if progress_made:
        case.turns_without_progress = 0
    else:
        case.turns_without_progress += 1

    # Escalate if stuck
    if case.turns_without_progress >= 3:
        enter_degraded_mode(case, DegradedModeType.NO_PROGRESS)

    return turn
```
