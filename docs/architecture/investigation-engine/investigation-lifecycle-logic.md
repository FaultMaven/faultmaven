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
4. [Supported Case Lifecycles](#4-supported-case-lifecycles)

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

**CONFIRMATION PATTERN (Conditional, Based on Context)**:

Confirmations reduce errors but create friction. Use conditional logic:

**WHEN TO CONFIRM** (two-step required):

- Situation is CRITICAL/HIGH severity (alignment crucial before action)
- Problem description is ambiguous, inconsistent, or incomplete
- Key details changed that affect investigation direction
- User manually requests status transition (via dropdown)
- First time transitioning to INVESTIGATING (establish shared understanding)

**WHEN TO SKIP CONFIRMATION** (natural progression):

- Problem already established and user asks follow-up question
- User reports results of agent-suggested action (implicit confirmation)
- Context is clear and user needs direct answer
- User provides information that refines (not changes) direction
- Investigation flowing naturally with user engagement

**Two-Step Confirmation Flow** (when required):

1. Agent presents what will happen (problem statement, action, etc.)
2. User explicitly confirms with Yes/No buttons or typed response

**Natural flow (Section 1.2)**:
- Turn N: User says "let's investigate"
- Turn N response: Agent presents problem statement + [Yes/No]
- Turn N+1: User clicks [Yes] or types confirmation
- Turn N+1 response: Agent transitions status

**Manual flow (Section 1.5)**:
- User clicks status dropdown → modal
- User confirms modal → sends system message
- Agent receives system message → presents statement + [Yes/No]
- User confirms → Agent transitions status

Both flows converge at the confirmation step.

```python
async def handle_inquiry_turn(case: Case, user_message: str) -> str:
    """
    Process inquiry turn and manage problem statement workflow.

    ITERATIVE REFINEMENT PATTERN (Section 1.7, line 773):
    1. Agent generates proposed_problem_statement from conversation
    2. Agent presents statement for confirmation
    3. User confirms OR provides corrections
    4. If corrections: Update proposed_problem_statement and repeat step 2
    5. If confirmed: Set problem_statement_confirmed = True
    """

    # Generate or update proposed_problem_statement
    if not case.inquiry.proposed_problem_statement or user_provides_corrections(user_message):
        case.inquiry.proposed_problem_statement = await llm_generate_problem_statement(
            conversation_history=case.messages,
            problem_confirmation=case.inquiry.problem_confirmation,
            user_corrections=extract_corrections(user_message)
        )

    # Check if user confirms statement
    if user_confirms(user_message):  # "Yes", "Yes, investigate", "That's right", etc.
        case.inquiry.problem_statement_confirmed = True
        case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
        case.inquiry.decided_to_investigate = True
        case.inquiry.decision_made_at = datetime.now(timezone.utc)

        # Now can_start_investigation returns True
        return await transition_to_investigating(case)

    else:
        # Present statement for confirmation
        return f"""Based on our conversation, the problem is:

{case.inquiry.proposed_problem_statement}

Is this what you want me to investigate?

[✅ Yes]  [❌ No]

💡 Tip: Click a button or type to clarify"""


def _apply_inquiry_updates(case: Case, updates: Any, metadata: Dict[str, Any]):
    """
    Handle structured updates during INQUIRY.

    Logic:
    1. If user confirms problem -> transition to INVESTIGATING
    2. If user provides preliminary guidance -> Refine problem statement
    3. If user decides to investigate -> Set flag
    """

    # 1. Capture problem statement
    if updates.proposed_problem_statement:
        case.inquiry.proposed_problem_statement = updates.proposed_problem_statement

    # 2. Check for transition
    if updates.problem_statement_confirmed and updates.decided_to_investigate:
        if not case.inquiry.proposed_problem_statement:
            # Error state: cannot confirm null statement
            return

        # Create ProblemVerification with confirmed statement
        case.problem_verification = ProblemVerification(
            symptom_statement=case.inquiry.proposed_problem_statement
            # LLM will fill other fields during investigation
        )

        transition_status(case, CaseStatus.INVESTIGATING, "system",
                         "User confirmed problem and decided to investigate")
```

#### INVESTIGATING → RESOLVED (Terminal)

**Trigger**: User-Agent Handshake (explicit user confirmation)

**User-Agent Handshake Pattern**:

Terminal transitions are NEVER automatic. The agent proposes resolution, and the
user must explicitly confirm before the transition executes.

**Flow**:
1. Agent detects solution effectiveness → includes `ProposedTransition` in response
2. System stores `pending_transition` on case (does NOT execute)
3. Agent's response asks user: "Should I mark this case as resolved?"
4. Next turn: user confirms → system sets `solution_verified=True` and transitions
5. If user declines → `pending_transition` cleared, investigation continues

**MULTIPLE SOLUTIONS HANDLING**:

If multiple solutions exist, the agent proposes resolution when AT LEAST ONE
solution appears effective. The user confirms which solution resolved the issue.

```python
def propose_transition(case, to_status, reason, summary, evidence_ids=None):
    """Store a pending transition proposal. Does NOT execute."""
    case.pending_transition = {
        "to_status": to_status,
        "reason": reason,
        "summary": summary,
        "evidence_ids": evidence_ids or [],
        "proposed_at": datetime.now(UTC).isoformat(),
        "proposed_by": "agent",
    }

def confirm_pending_transition(case, user_id):
    """Execute transition after user confirms."""
    case.progress.solution_verified = True
    case.atomic_update(
        status=CaseStatus.RESOLVED,
        resolved_at=datetime.now(UTC),
        closed_at=datetime.now(UTC),
        closure_reason="resolved",
    )
    case.pending_transition = None
    # TERMINAL - no further transitions
```

**Why not automatic?** The LLM's interpretation of "it works" can be wrong.
The user might mean "this command works" not "the whole system is fixed."
Terminal transitions are irreversible, so false positives are costly.

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

> **Implementation Status:** Design complete, wiring deferred. The `KnowledgeResolution` model exists in contracts and the `InquiryResponse.knowledge_resolution` field is in the schema, but `_process_response_structured()` does not yet process this field. See limitation G5 in [Opportunistic Investigation Framework](./opportunistic-investigation-framework.md#known-limitations--deferred-items).

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
2. INQUIRY: Agent says "This looks similar to [past case]. Solution was [X]. Try this?"
3. INQUIRY: User tries solution
4. INQUIRY: User confirms "Yes, that fixed it!" (explicit confirmation - follows two-step pattern)
5. System: Transition directly to RESOLVED (skip INVESTIGATING)
```

**FAST-TRACK CONFIRMATION PATTERN**:

The fast-track resolution uses the same two-step confirmation pattern:

1. Agent detects KB match during INQUIRY
2. Agent presents: "Similar past case [X]. Solution: [Y]. Try this?"
3. User tries solution
4. User confirms: "Yes, that fixed it!" (explicit confirmation)
5. Agent transitions directly to RESOLVED (skips INVESTIGATING)

Confirmation follows same two-step pattern as INQUIRY → INVESTIGATING.

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

### 1.4 Automatic Terminal Transitions

Terminal transitions are triggered automatically based on milestone completion.

```python
async def process_turn(case: Case, user_message: str) -> str:
    """
    Process one turn and update milestones.

    AUTOMATIC TRANSITIONS:
    - Checked AFTER agent response generation
    - Triggered by milestone completion (data-driven)
    - Terminal transitions are irreversible
    """

    # Validate not terminal
    if case.is_terminal:
        return "Case is closed. No further updates allowed."

    # Capture state before
    progress_before = case.progress.dict()

    # Agent analyzes available data and completes tasks
    agent_response = await agent.process(case, user_message)

    # Capture state after
    progress_after = case.progress.dict()

    # Detect completed milestones
    milestones_completed = detect_milestone_completions(progress_before, progress_after)

    # Record turn
    record_turn(case, milestones_completed)

    # ============================================================
    # TERMINAL TRANSITION HANDLING (User-Agent Handshake)
    # ============================================================
    # Terminal transitions are NEVER automatic. The agent proposes
    # a transition via ProposedTransition, and the system holds it
    # pending until the user confirms in the next turn.

    # 1. Handle pending transition confirmation from previous turn
    if case.pending_transition:
        if user_confirms_transition(user_message):
            confirm_pending_transition(case, case.user_id)
        elif user_declines_transition(user_message):
            cancel_pending_transition(case)

    # 2. Handle ProposedTransition from LLM response
    proposed = getattr(response.state_updates, "proposed_transition", None)
    if proposed:
        propose_transition(
            case=case,
            to_status=proposed.to_status,
            reason=proposed.reason,
            summary=proposed.summary,
            evidence_ids=proposed.evidence_ids,
        )

    return agent_response


# Terminal transitions (all require user confirmation):
#
# INVESTIGATING → RESOLVED:
#   - Trigger: Agent proposes via ProposedTransition + user confirms
#   - Automatic: No (requires User-Agent Handshake)
#   - Terminal: Yes (irreversible)
#
# INVESTIGATING → CLOSED:
#   - Trigger: User explicit action (force_close via UI or chat)
#   - Automatic: No (requires user intent)
#   - Terminal: Yes (irreversible)
#
# INQUIRY → CLOSED:
#   - Trigger: User explicit action (close_from_inquiry)
#   - Terminal: Yes (irreversible)
#
# INQUIRY → RESOLVED:
#   - Trigger: Fast-track KB resolution + user confirmation
#   - Terminal: Yes (irreversible)


# ============================================================
# EXPLICIT USER-TRIGGERED TRANSITIONS (Non-Automatic)
# ============================================================

def force_close_investigation(case: Case, user_id: str, reason: str):
    """
    User explicitly abandons investigation without solution.

    Trigger: User action (not automatic)
    Terminal: Yes (irreversible)
    """
    if case.status != CaseStatus.INVESTIGATING:
        raise InvalidTransitionError("Can only force-close from INVESTIGATING status")

    case.status = CaseStatus.CLOSED
    case.closed_at = datetime.now(timezone.utc)
    case.closure_reason = reason  # "abandoned" | "escalated" | "other"
    case.status_history.append(CaseStatusTransition(
        from_status=CaseStatus.INVESTIGATING,
        to_status=CaseStatus.CLOSED,
        triggered_at=datetime.now(timezone.utc),
        triggered_by=user_id,
        reason=f"User force-closed: {reason}"
    ))
    # TERMINAL - no further transitions


def close_from_inquiry(case: Case, user_id: str):
    """
    Close after inquiry without formal investigation.

    Trigger: User action (not automatic)
    Terminal: Yes (irreversible)
    """
    if case.status != CaseStatus.INQUIRY:
        raise InvalidTransitionError("Can only close-from-inquiry when in INQUIRY status")

    case.status = CaseStatus.CLOSED
    case.closed_at = datetime.now(timezone.utc)
    case.closure_reason = "inquiry_only"
    case.status_history.append(CaseStatusTransition(
        from_status=CaseStatus.INQUIRY,
        to_status=CaseStatus.CLOSED,
        triggered_at=datetime.now(timezone.utc),
        triggered_by=user_id,
        reason="User closed after inquiry only"
    ))
    # TERMINAL - no further transitions
```

#### 1.4.1 State Update Timing

State updates occur at specific points within a turn to ensure consistency:

| Update Type | When | Trigger |
|-------------|------|---------|
| `proposed_problem_statement` | During INQUIRY turn | LLM generates from conversation |
| `problem_statement_confirmed` | After user confirmation | User says "Yes" or equivalent |
| `symptom_verified` | After evidence processing | Evidence validates symptom |
| `path_selection` | After `symptom_verified = True` | Automatic |
| `mitigation_applied` | During MITIGATION_FIRST path | After user confirms mitigation worked |
| `root_cause_identified` | After hypothesis validation | Strong evidence supports hypothesis |
| `solution_verified` | After user confirms fix | User confirms problem resolved |
| Terminal transition | End of turn | After all other processing |

**Order of Operations Within a Turn**:

1. **Receive user message**
2. **LLM processes** and generates response + `state_updates`
3. **Apply non-terminal state updates** (milestones, evidence, hypotheses)
4. **Record turn progress** (detect what changed)
5. **Check terminal transitions** (RESOLVED/CLOSED) if conditions met
6. **Return response to user**

**Rationale**: Terminal transitions happen last to ensure all state is consistent before case becomes immutable.

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

### 1.6 Agent Role Constraints

**CRITICAL**: The agent is an **ADVISOR**, not an executor. It helps users troubleshoot but never performs actions itself.

#### What the Agent Can Do

- **Suggest** actions for users to take
- **Ask** for data users can provide
- **Recommend** diagnostic steps
- **Explain** reasoning and implications
- **Guide** through investigation methodology

#### What the Agent CANNOT Do

- Execute commands or queries
- Access systems, logs, or metrics directly
- Run diagnostic tools
- Make changes to infrastructure

#### Language Constraints

**Prohibited Phrases** (implies agent execution):

- ❌ "Let me check the logs"
- ❌ "I'll execute that command"
- ❌ "I'll look into the database"
- ❌ "Let me run a query"
- ❌ "Which would you like me to run?"

**Correct Phrases** (advisor tone):

- ✅ "Could you check the logs for errors?"
- ✅ "You might want to try running that command"
- ✅ "It would help to look at the database metrics"
- ✅ "You could run a query to confirm"
- ✅ "Which would you like to try first?"

**Rationale**: This constraint ensures user expectations align with actual capabilities. Users who think the agent is executing commands will become frustrated when nothing happens.

**Implementation**: LLM system prompts must explicitly state advisor-only role and prohibit execution language.

---

## 2. Path Selection & Routing

### 2.0 Path Selection Timeline (3 Phases)

Path selection happens in THREE distinct phases to balance early urgency detection with accurate routing:

#### Phase 1: Preliminary Assessment (INQUIRY Status)

**When**: Turn 1-2, during problem confirmation

**Purpose**: Early urgency detection for user awareness

**Output**: `preliminary_urgency` (stored but not used for routing yet)

```python
def assess_preliminary_urgency(case: Case) -> PreliminaryUrgency:
    """
    Early urgency assessment during INQUIRY.
    Provides early warning but does NOT determine path yet.

    Called: During first turn when problem_confirmation is created
    """
    return PreliminaryUrgency(
        level=llm_assess_urgency(case.inquiry.problem_confirmation),
        is_ongoing=llm_detect_temporal_state(case.inquiry.problem_confirmation),
        impact_assessment="Business impact description",
        mitigation_hint="Optional quick mitigation suggestion"
    )
```

**CRITICAL Signals**:
- "revenue loss", "production downtime", "data loss/corruption"
- "100% error rate", "total service failure", "security breach"

**HIGH Signals**:
- "customers affected", "checkout failing", "payments broken"
- "30%+ of requests failing", "customer complaints", "SLA violation"

**MEDIUM Signals**:
- "intermittent issues", "some users affected", "partial failure"
- "slow but functional", "degraded experience", "occasional errors"
- "10-30% failure rate", "performance issues", "latency spike"

**LOW Signals**:
- "historical investigation", "post-mortem", "retrospective"
- "optimization opportunity", "nice to have", "not urgent"
- "minor bug", "cosmetic issue", "edge case"

**Detection Timing**: Urgency signals should be detected in Turn 1 and acknowledged immediately in agent response. Don't wait for formal path selection to recognize urgency.

**Early Path Hint** (during INQUIRY):
If CRITICAL/HIGH + ONGOING detected, agent offers:
> "This sounds like it's actively impacting users. Should I focus on quick
> mitigation first, then investigate root cause after?"

This accelerates path selection without waiting for full verification.

#### Phase 2: Formal Path Selection (INVESTIGATING Status)

**When**: First turn AFTER `symptom_verified = True`

**Purpose**: Determine investigation path based on verified urgency

**Output**: `case.path_selection` (used for routing)

```python
def select_investigation_path(case: Case) -> PathSelection:
    """
    Formal path selection after symptom verification complete.

    Called: Automatically when symptom_verified transitions False → True
    Precondition: case.problem_verification with temporal_state and urgency_level set
    """
    if not case.progress.symptom_verified:
        raise InvalidStateError("Cannot select path before symptom verification")

    return determine_investigation_path(case.problem_verification)
```

#### Phase 3: Path Execution (INVESTIGATING Status)

**When**: Immediately after path selection

**Purpose**: Apply path-specific behavior (mitigation for MITIGATION_FIRST)

**Output**: `mitigation_applied = True` (if applicable)

```python
async def execute_path_behavior(case: Case):
    """
    Execute path-specific behavior immediately after selection.

    For MITIGATION_FIRST: Apply mitigation if correlation strong enough
    For ROOT_CAUSE: Continue to hypothesis formulation
    """
    if case.path_selection.path == InvestigationPath.MITIGATION_FIRST:
        if case.problem_verification.correlation_confidence >= 0.7:
            # Strong correlation → apply mitigation
            await apply_mitigation(case)
            case.progress.mitigation_applied = True
```

**Timeline Diagram**:

```
Turn 1 (INQUIRY):     preliminary_urgency assessed → Early hint provided
Turn 2 (INQUIRY→INVESTIGATING): Status transition
Turn 3 (INVESTIGATING): symptom_verified = True → path_selection determined → mitigation applied (if MITIGATION_FIRST)
```

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
  - Next: Agent offers user the choice (see below)

- **Post-Mitigation Decision Point**
  After mitigation is applied and verified effective, the agent asks the user
  whether to continue with root cause analysis or close the case. See Section 4.4
  for the two sub-scenarios (Full Path vs. Quick Path).

- **If user chooses RCA (Full Path):**
  - **Stage 2: Hypothesis Formulation**
    - Generate theories about root cause
    - Service is now stable, can take time for thorough analysis
  - **Stage 3: Hypothesis Validation**
    - Test hypotheses with diagnostic evidence
    - Mark `root_cause_identified = True`
  - **Stage 4: Solution**
    - Apply permanent fix, verify effectiveness
    - Agent proposes resolution via User-Agent Handshake

- **If user chooses to close (Quick Path):**
  - Agent proposes resolution via User-Agent Handshake
  - Case transitions to RESOLVED with mitigation as the resolution

**Full Path Milestones**: `symptom_verified` → `mitigation_applied` → `mitigation_verified` → `root_cause_identified` → `solution_applied` → `solution_verified`

**Quick Path Milestones**: `symptom_verified` → `mitigation_applied` → `mitigation_verified` → `solution_verified`

**Mitigation Follow-up Guidance (Advisory)**

When a temporary workaround is applied, the agent SHOULD advise the user about
follow-up even if they choose the quick path:

> "The fraud check bypass stopped the immediate issue. If you close this case,
> remember to re-enable the fraud check once the SSL cert is renewed. Would you
> like help investigating why the cert wasn't monitored for expiration?"

This is advisory — the system does not block resolution if the user declines RCA.

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

### 3.1 Evidence Milestone Validation

The LLM structured output is the **sole authority** for milestone advancement.
When the LLM claims a milestone has been reached (via the `milestones` field in
its response schema), the evidence processor validates the claim against cited
evidence. It does NOT independently advance milestones.

**Design Decision (Issue A)**: The evidence processor was previously a
keyword-based discovery layer that parsed LLM-generated analysis text to find
milestones. This created a dual pathway for advancement and was fragile. It is
now validation-only.

```python
def validate_milestone_claims(
    case: Case,
    milestones_claimed: List[str],
    reasoning: Optional[InternalReasoning] = None,
) -> List[MilestoneValidationResult]:
    """
    Validate that LLM milestone claims are supported by cited evidence.

    This does NOT advance milestones. It checks whether the LLM's claims
    are justified by the evidence IDs cited in internal_reasoning.

    Called: After LLM sets milestones in structured output
    """

    for milestone in milestones_claimed:
        expectations = MILESTONE_EVIDENCE_EXPECTATIONS[milestone]

        # Count evidence in expected categories among cited IDs
        relevant = count_cited_evidence(case, reasoning, expectations)

        if relevant < expectations["min_evidence"]:
            log_warning(
                f"Milestone '{milestone}' claimed with {relevant} relevant evidence "
                f"(expected >= {expectations['min_evidence']})"
            )

# Minimum evidence expectations per milestone:
MILESTONE_EVIDENCE_EXPECTATIONS = {
    "symptom_verified":     {"min_evidence": 1, "categories": [SYMPTOM_EVIDENCE]},
    "scope_assessed":       {"min_evidence": 1, "categories": [SYMPTOM_EVIDENCE]},
    "timeline_established": {"min_evidence": 1, "categories": [SYMPTOM_EVIDENCE]},
    "changes_identified":   {"min_evidence": 1, "categories": [SYMPTOM_EVIDENCE, CAUSAL_EVIDENCE]},
    "root_cause_identified":{"min_evidence": 2, "categories": [CAUSAL_EVIDENCE]},
    "solution_proposed":    {"min_evidence": 1, "categories": [CAUSAL_EVIDENCE]},
    "solution_applied":     {"min_evidence": 0, "categories": []},  # User confirmation
    "mitigation_applied":   {"min_evidence": 0, "categories": []},  # User confirmation
}
# NOTE: solution_verified is not in this list — it requires the User-Agent Handshake.
```

**Evidence Classification**:

Evidence is created after LLM evaluation with a specific category assigned.
See [Evidence Classification Design](../data-processing/evidence-classification-design.md) for complete details.

| Category | Description |
|----------|-------------|
| `SYMPTOM_EVIDENCE` | Verifies symptoms, scope, timeline, changes |
| `CAUSAL_EVIDENCE` | Tests hypotheses about root cause |
| `RESOLUTION_EVIDENCE` | Verifies solution effectiveness |
| `CONTEXTUAL_EVIDENCE` | Provides baseline/environmental context |
| `REJECTED` | Analyzed but not useful for investigation |

```

### 3.2 Turn Recording and Progress Detection

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

    # Detect hypotheses generated this turn
    hypotheses_count_before = len([h for h in case.hypotheses.values() if h.created_at < turn_start_time])
    hypotheses_count_after = len(case.hypotheses)
    hypotheses_generated = hypotheses_count_after - hypotheses_count_before

    # Detect solutions proposed this turn
    solutions_count_before = len([s for s in case.solutions if s.proposed_at < turn_start_time])
    solutions_count_after = len(case.solutions)
    solutions_proposed = solutions_count_after - solutions_count_before

    # Determine if progress made
    progress_made = (
        len(milestones_completed) > 0 or
        len(evidence_added) > 0 or
        hypotheses_generated > 0 or  # NEW: Generating hypotheses is progress
        any(h.status == HypothesisStatus.VALIDATED for h in case.hypotheses.values()) or
        solutions_proposed > 0  # NEW: Proposing solutions is progress
    )

    # RATIONALE:
    # Hypothesis generation and solution proposals represent active agent work.
    # Only increment turns_without_progress when TRULY stuck (no actions taken).
    # Otherwise, waiting for user evidence would trigger premature degraded mode.

    # ============================================================
    # PROGRESS DEFINITION (for turns_without_progress counter)
    # ============================================================
    #
    # Progress IS made when ANY of the following occur:
    # - Milestone transitions False → True
    # - Evidence is added to the case
    # - New hypothesis is generated
    # - Hypothesis status changes (ACTIVE → VALIDATED/REFUTED)
    # - Solution is proposed or verified
    # - User confirms problem statement or path selection
    #
    # Progress is NOT made when:
    # - Agent only asks clarifying questions (no state advancement)
    # - Agent repeats previous information
    # - Conversation is off-topic or circular
    # - User provides information that doesn't advance investigation
    #
    # IMPORTANT: Waiting for user to provide requested evidence does NOT
    # count against progress. The counter only increments when the agent
    # fails to take productive action or generate useful hypotheses.

    # Create turn record
    turn = TurnProgress(
        turn_number=case.current_turn,
        milestones_completed=milestones_completed,
        evidence_added=evidence_added,
        progress_made=progress_made,
        # Updated logic: Robust outcome determination based on milestones, evidence, hypotheses
        outcome=self._determine_turn_outcome(case, metadata, outcome_override="conversation")
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

    # Exit degraded mode if progress resumes
    check_degraded_mode_exit(case, progress_made)

    return turn


def check_degraded_mode_exit(case: Case, progress_made: bool):
    """
    Exit degraded mode when progress resumes.

    Entry: turns_without_progress >= 3
    Exit: progress_made = True on any subsequent turn

    DEGRADED MODE RECOVERY:
    - Agent enters degraded mode after 3 turns without progress
    - Agent exits automatically when progress resumes
    - Exit condition: Any milestone, evidence, hypothesis, or solution activity
    - Recovery resets turns_without_progress counter to 0
    """
    if case.degraded_mode and case.degraded_mode.is_active:
        if progress_made:
            case.degraded_mode.exited_at = datetime.now(timezone.utc)
            case.degraded_mode.exit_reason = "Progress resumed - user provided evidence or agent found breakthrough"
            case.degraded_mode.is_active = False
            case.turns_without_progress = 0  # Reset counter


def determine_turn_outcome(case: Case, progress_made: bool) -> TurnOutcome:
    """
    Determine turn outcome classification.

    Checked AFTER milestone detection and evidence processing.
    Used for LLM observability and metrics (not workflow control).
    """

    # Terminal transition
    if case.is_terminal:
        return TurnOutcome.CASE_RESOLVED if case.status == CaseStatus.RESOLVED else TurnOutcome.OTHER

    # Milestone completed
    if any(milestone_completed_this_turn(case)):
        return TurnOutcome.MILESTONE_COMPLETED

    # Hypothesis validated
    if any(h.tested_at == case.current_turn for h in case.hypotheses.values()):
        return TurnOutcome.HYPOTHESIS_TESTED

    # Evidence provided
    if any(e.collected_at_turn == case.current_turn for e in case.evidence):
        return TurnOutcome.DATA_PROVIDED

    # Agent requested data
    if agent_requested_data_this_turn(case):
        return TurnOutcome.DATA_REQUESTED

    # Conversation only
    return TurnOutcome.CONVERSATION
```

### 3.3 Diagnostic Reasoning Requirements

**REQUIREMENT**: Before suggesting any action, mitigation, or hypothesis, the agent MUST demonstrate context-specific diagnostic reasoning that explains WHY the suggestion is relevant to this specific investigation.

**PURPOSE**: Prevent generic advice disconnected from case evidence. Force grounded reasoning based on actual symptoms, timeline, and context.

---

**Diagnostic Reasoning Pattern** (agent response structure):

1. **Observation** - What specific evidence/symptom triggered this line of thinking?
2. **Analysis** - Why does this evidence point toward this explanation/action?
3. **Suggestion** - What specific action follows from this reasoning?
4. **Expected Outcome** - What should happen if this reasoning is correct?

---

**Example: BAD (Generic Advice)**

```
❌ Agent: "Your database is slow. You should:
   - Scale up your database pods
   - Add read replicas
   - Enable connection pooling
   - Check for slow queries"
```

**Why Bad**: No connection to this specific case. Could apply to any database issue. No diagnostic reasoning.

---

**Example: GOOD (Grounded Diagnostic Reasoning)**

```
✅ Agent: "I notice the timeouts started exactly 2 minutes after deployment e8f3a92:

OBSERVATION:
- Error rate jumped from 0% to 40% at 14:03 UTC
- Deployment e8f3a92 completed at 14:01 UTC
- Timeline correlation: +2 minutes
- No infrastructure changes in that window

ANALYSIS:
This tight temporal correlation (2-minute gap) suggests the deployment
introduced the issue rather than database capacity. If this were a capacity
problem, we'd expect gradual degradation, not a sharp cliff at deployment time.

SUGGESTED MITIGATION:
Rollback deployment e8f3a92 to previous version (d4b2c11) to test causation.

EXPECTED OUTCOME:
If my reasoning is correct:
- Error rate should drop from 40% to near-0% within 60 seconds of rollback
- Connection timeouts should stop
- If errors persist after rollback, we can rule out the deployment"
```

**Why Good**:
- Grounds reasoning in specific evidence (timestamps, deployment ID, error rate)
- Explains WHY correlation matters (sharp vs gradual)
- Mitigation is testable and reversible
- Defines success criteria

---

**Example: BAD (Hypothesis without Reasoning)**

```
❌ Agent: "Based on the symptoms, here are three hypotheses:
   1. Database connection pool exhaustion
   2. Network timeout misconfiguration
   3. Deadlock in transaction processing"
```

**Why Bad**: Lists possibilities without explaining which evidence points where. No prioritization or diagnostic logic.

---

**Example: GOOD (Hypothesis with Diagnostic Reasoning)**

```
✅ Agent: "From the evidence, I'm forming a primary hypothesis:

OBSERVATION:
- Logs show 'connection timeout after 5000ms' errors
- Database metrics show connection pool at 45/100 (not exhausted)
- No slow query logs during error window
- Application default timeout: 5 seconds
- Database query latency: 8-12 seconds (above timeout)

ANALYSIS:
The database isn't refusing connections (pool not full), but queries are
taking 8-12 seconds while the app times out at 5 seconds. This explains
why we see timeouts despite available capacity.

PRIMARY HYPOTHESIS:
Recent deployment introduced N+1 query pattern causing queries to take
8-12 seconds, exceeding application's 5-second timeout threshold.

DIAGNOSTIC TEST:
Can you share the query execution plan for the slow queries? If we see
sequential scans or missing JOINs that weren't present before deployment
e8f3a92, that would validate this hypothesis."
```

**Why Good**:
- Uses specific metrics (5s timeout vs 8-12s latency)
- Rules out competing explanations (pool exhaustion)
- Hypothesis is testable and falsifiable
- Requests specific evidence to validate

---

**PROHIBITED PATTERNS** (agent must NEVER do these):

❌ **Checklist Engineering**: "Try these 10 things and see what works"
❌ **Solution Brainstorming**: "Here are 5 possible solutions..."
❌ **Generic Best Practices**: "You should implement monitoring/logging/alerting"
❌ **Speculation Without Evidence**: "It's probably a memory leak or DNS issue"
❌ **Action Without Explanation**: "Run this command: `kubectl restart deployment foo`"

---

**REQUIRED PATTERNS** (agent must ALWAYS do these):

✅ **Evidence-Grounded**: Quote specific metrics, timestamps, log lines, error messages
✅ **Causal Reasoning**: Explain mechanism (HOW would X cause Y?)
✅ **Falsifiable**: Define what evidence would prove hypothesis wrong
✅ **Prioritized**: Explain why this hypothesis over alternatives
✅ **Testable**: Suggest specific evidence that would validate/refute

---

**ENFORCEMENT**:

This requirement applies to ALL agent suggestions during INVESTIGATING state:
- Mitigation proposals (MITIGATION_FIRST path)
- Hypothesis generation (all paths)
- Diagnostic command suggestions
- Solution proposals
- Evidence requests (explain WHY specific evidence is needed)

**EXCEPTION**: INQUIRY state (problem statement refinement) does not require diagnostic reasoning, as investigation hasn't started yet.

---

## 4. Supported Case Lifecycles

This section outlines all possible case lifecycles and their associated milestones.

### 4.1 Inquiry-Only Lifecycle (No Investigation)
**User Goal**: Ask a quick question or get clarification without starting a formal investigation.
**Flow**: `INQUIRY` → `CLOSED`

#### Workflow Steps
1.  **User Inquiry**: User asks a question (e.g., "How do I check logs?").
2.  **Agent Response**: Agent answers the question.
3.  **Closure**: User leaves or explicitly closes the session.

#### Milestones
*   None (Investigation milestones do not start).

---

### 4.2 Fast-Track Resolution (Knowledge Base Match)
**User Goal**: Resolve a known issue quickly using past cases or runbooks.
**Flow**: `INQUIRY` → `RESOLVED` (Skips `INVESTIGATING`)

#### Workflow Steps
1.  **Detection**: Agent detects high-confidence Knowledge Base match during Inquiry.
2.  **Proposal**: Agent suggests the known solution from the KB match.
3.  **Verification**: User tries the solution and confirms it works.
4.  **Transition**: Case transitions directly to `RESOLVED`.

#### Milestones
*   `knowledge_resolution` (Record of KB match application)

---

### 4.3 Standard Investigation (Root Cause Path)
**User Goal**: Diagnosing a new or complex issue to find the root cause and fix it permanently.
**Flow**: `INQUIRY` → `INVESTIGATING` → `RESOLVED`

#### Workflow Steps & Milestones

**Phase 1: Inquiry**
*   **Goal**: Establish problem statement.
*   **Transition Trigger**: User confirms problem statement and decides to investigate.

**Phase 2: Investigation (Iterative)**
The agent pursues milestones opportunistically.

*   **Stage A: Understanding (Symptom Verification)**
    *   `symptom_verified`: Confirmed via evidence (logs, metrics).
    *   `scope_assessed`: Impact radius determined (e.g., "All regions", "Prod only").
    *   `timeline_established`: Start time and duration identified.
    *   `changes_identified`: Recent deployments or config changes mapped.

*   **Stage B: Diagnosing (Hypothesis & Root Cause)**
    *   `root_cause_identified`: Validated hypothesis explaining the issue.
        *   *Method*: `hypothesis_validation` or `direct_analysis`.

*   **Stage C: Resolving (Solution)**
    *   `solution_proposed`: Agent suggests a fix (e.g., "Scale up replicas").
    *   `solution_applied`: User confirms action taken.
    *   `solution_verified`: Evidence confirms system recovery.

**Phase 3: Resolution**
*   **Transition Trigger**: `solution_verified` is True.
*   **State**: `RESOLVED`.

---

### 4.4 Mitigation-First Investigation (Ongoing Outage)
**User Goal**: Restore service availability immediately.
**Trigger**: High Severity + Ongoing Outage (auto-selected or user-chosen path).

After mitigation is applied and verified, the user decides whether to continue
with root cause analysis or close the case. The system does not distinguish
between these sub-scenarios — the milestone state captures what happened.

#### Sub-Scenario A: Full Path (Mitigation + RCA)
**Flow**: `INQUIRY` → `INVESTIGATING` (Mitigation → RCA) → `RESOLVED`

The user wants a permanent fix after the immediate fire is out.

**Milestones**:
*   `symptom_verified`: Problem confirmed.
*   `mitigation_applied`: Temporary fix applied (e.g., rollback, restart).
*   `mitigation_verified`: Service restored (root cause still unknown).
*   `root_cause_identified`: Why did it fail?
*   `solution_proposed`: Permanent fix proposed.
*   `solution_applied`: Permanent fix deployed.
*   `solution_verified`: Permanent fix validated (via User-Agent Handshake).

#### Sub-Scenario B: Quick Path (Mitigation Only)
**Flow**: `INQUIRY` → `INVESTIGATING` (Mitigation) → `RESOLVED`

The user is satisfied with the mitigation and does not need RCA. This is a
valid outcome — not every incident requires a root cause investigation.

**Milestones**:
*   `symptom_verified`: Problem confirmed.
*   `mitigation_applied`: Temporary fix applied.
*   `mitigation_verified`: Service restored.
*   `solution_verified`: User confirms resolution (via User-Agent Handshake).
*   `root_cause_identified`: **Not set** (no RCA performed).

#### Agent Behavior After Mitigation

After `mitigation_verified = True`, the agent MUST offer the user a choice:

> "The mitigation is working — [specific metric showing improvement]. Would you
> like me to help investigate the root cause to prevent recurrence, or is this
> sufficient to close the case?"

The agent should factor in `mitigation_effectiveness`:
- **1.0 (fully resolved)**: Both options equally valid. Offer choice neutrally.
- **0.5-0.9 (partially resolved)**: Recommend RCA, noting residual risk.
- **< 0.5 (mostly ineffective)**: Strongly recommend continuing investigation.

#### How the System Distinguishes the Two Paths (Retrospectively)

No additional code, data elements, or routing logic is needed. The milestone
state IS the distinction:

| Field | Full Path | Quick Path |
|-------|-----------|------------|
| `mitigation_applied` | True | True |
| `mitigation_verified` | True | True |
| `root_cause_identified` | True | False |
| `solution_applied` | True | False |
| `solution_verified` | True | True |

Analytics and reporting can query these milestone combinations to classify
resolved cases by path type after the fact.

---

### 4.5 Abandoned / Escalated Investigation
**User Goal**: Investigation stalled or handed off to human expert.
**Flow**: `INQUIRY` → `INVESTIGATING` → `CLOSED`

#### Workflow Steps
1.  **Investigation Starts**: Milestones partially completed.
2.  **Stall/Escalation**:
    *   Agent cannot find root cause.
    *   User stops responding.
    *   User explicitly requests escalation.
3.  **Closure**: Case marked `CLOSED` with reason (e.g., `escalated`, `abandoned`).

#### Milestones
*   Partial completion of verification/diagnosis milestones.
*   `working_conclusion`: Summary of findings up to the point of closure.
