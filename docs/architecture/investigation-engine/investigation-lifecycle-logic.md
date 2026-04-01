# Investigation Lifecycle Logic

This document defines the state transitions, path routing, and turn tracking logic for FaultMaven's evidence-driven investigation framework.

**Related Documents**:
- [Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md) - Overview and philosophy
- [Investigation Data Models](./investigation-data-models.md) - Core data structures

---

## Table of Contents

1. [Investigation Lifecycle](#1-investigation-lifecycle)
2. [Path Selection & Routing](#2-path-selection--routing)
3. [Turn Progress Tracking](#3-turn-progress-tracking)
4. [Supported Case Lifecycles](#4-supported-case-lifecycles)

---

## 1. Investigation Lifecycle

### 1.1 Case Action Map

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
       │                              │ Diagnosing         │
       │                              │ Mitigating         │
       │                              │ Resolving          │
       │                              └─────────┬──────────┘
       │                                        │
       │                              ┌─────────┴──────────┐
       │                              │                    │
       │                   (solution_verified)    (no solution,
       │                              │            abandoned/escalated/
       │                              │            mitigation_sufficient)
       │                              ▼                    ▼
       │                      ┌──────────────┐    ┌──────────────┐
       │                      │   RESOLVED   │    │    CLOSED    │
       │                      │              │    │              │
       │                      │ DISPOSITION  │    │ DISPOSITION  │
       │                      │ With solution│    │ No solution  │
       │                      └──────────────┘    └──────────────┘
       │                                                  ▲
       └──(no investigation needed)──────────────────────┘
          (inquiry-only)
```

### 1.2 Case Actions

#### INQUIRY → INVESTIGATING

**Trigger**: User commits to formal investigation AND confirms problem statement

**CONFIRMATION PATTERN (Conditional, Based on Context)**:

Confirmations reduce errors but create friction. Use conditional logic:

**WHEN TO CONFIRM** (two-step required):

- Situation is CRITICAL/HIGH severity (alignment crucial before action)
- Problem description is ambiguous, inconsistent, or incomplete
- Key details changed that affect investigation direction
- User manually requests case action (via dropdown)
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


def _apply_inquiry_updates(case: Case, updates: Any, metadata: Dict[str, Any],
                           user_message: str = ""):
    """
    Handle structured updates during INQUIRY.

    Logic:
    1. If LLM detects user confirmation -> transition to INVESTIGATING
    2. If LLM misses confirmation but user_confirms() matches -> mechanical fallback
    3. If user provides preliminary guidance -> Refine problem statement
    4. If user decides to investigate -> Set flag

    The mechanical fallback (step 2) uses a word-boundary regex matcher with a
    100-char message length guard (inline in milestone_engine.py) to catch
    explicit confirmations ("yes", "proceed", "looks good") that the LLM missed.
    This prevents the INQUIRY confirmation loop where the agent re-asks "Let me
    confirm..." across multiple turns without progressing.
    """

    # 1. Capture problem statement
    if updates.proposed_problem_statement:
        case.inquiry.proposed_problem_statement = updates.proposed_problem_statement

    # 2. Check for transition (LLM path)
    if updates.user_confirmed_investigation and case.inquiry.proposed_problem_statement:
        case.inquiry.problem_statement_confirmed = True
        case.inquiry.decided_to_investigate = True
        # ... transition fires via _check_automatic_transitions

    # 2b. Mechanical fallback: LLM missed confirmation, but user message matches
    elif (not updates.user_confirmed_investigation
          and case.inquiry.proposed_problem_statement
          and not case.inquiry.problem_statement_confirmed
          and user_confirms(user_message)):
        case.inquiry.problem_statement_confirmed = True
        case.inquiry.decided_to_investigate = True
        # Same transition path as above
```

#### 1.2.1 Evidence Classification Lifecycle

Evidence classification is **content-based, not stage-based** (see Section 5.2 of [evidence-driven-investigation-framework.md](./evidence-driven-investigation-framework.md)). The LLM evaluates data and classifies it by what it contains, not by which state the case is in.

**Core principles:**

1. **Uploads create UploadedFile only** — file uploads create raw metadata (`UploadedFile`) in any state. Evidence records are created by the LLM via `evidence_to_add` when it evaluates the data during its analysis turn.
2. **Classification is content-based** — error logs are `SYMPTOM_EVIDENCE` whether submitted during INQUIRY or INVESTIGATING. Normal configs are `CONTEXTUAL_EVIDENCE`. The LLM classifies based on what the data contains, not which state the case is in.
3. **Contextual evidence is a classification judgment** — `CONTEXTUAL_EVIDENCE` means the LLM evaluated the data and found it irrelevant to the problem at hand (e.g., normal baseline configs when investigating an OOM crash). It is not a default placeholder.
4. **Milestones emerge from evidence classification** — milestones are a natural consequence of evidence categories, not LLM-driven during transitions. `_infer_milestones()` maps evidence categories to eligible milestones via `CATEGORY_MILESTONE_MAP`.

**How it works at INQUIRY → INVESTIGATING transition:**

- During INQUIRY, the LLM may create evidence via `evidence_to_add` with content-appropriate categories. These have `advances_milestones=[]` because milestone tracking is not active during INQUIRY.
- At transition, retroactive attribution runs for ALL evidence:
  - Contextual evidence gets `[]` from `_infer_milestones()` because `CATEGORY_MILESTONE_MAP[CONTEXTUAL_EVIDENCE] = []`.
  - Categorized evidence (e.g., `SYMPTOM_EVIDENCE`) gets milestones attributed based on category.
- This applies uniformly — no distinction between manual and natural flow transitions.

**Manual vs natural flow — implicit distinction:**

- **Manual flow**: User transitions via status dropdown. Typically no evidence or only contextual evidence exists → 0 milestones attributed (natural consequence, no special flags).
- **Natural flow**: LLM detected a problem and created categorized evidence during INQUIRY → milestones attributed from categories at transition.

The distinction is implicit in the evidence state, not enforced by a `manual` flag.

**Data layers:**

```text
Upload time:   UploadedFile (raw file metadata)
LLM analysis:  Evidence created via evidence_to_add with content-based category
Transition:    Retroactive milestone attribution from evidence categories
```

**Validation:**

`validate_reasoning_first` checks for non-contextual (actionable) evidence when the LLM attempts to complete milestones. Contextual evidence alone cannot justify milestone claims — the LLM must first have evaluated and classified data into actionable categories (SYMPTOM, CAUSAL, MITIGATION, or SOLUTION).

#### INVESTIGATING → RESOLVED (Disposition)

**Trigger**: User-Agent Handshake (explicit user confirmation)

**User-Agent Handshake Pattern**:

Disposition actions are NEVER automatic. The agent proposes resolution, and the
user must explicitly confirm before the case action executes.

**Flow**:
1. Agent detects solution effectiveness → includes `ProposedTransition` in response
2. System stores `pending_transition` on case (does NOT execute)
3. Agent's response asks user: "Should I mark this case as resolved?"
4. Next turn: user confirms → system ensures milestone ordering (`solution_proposed` → `solution_accepted` → `solution_verified`) and transitions
5. If user declines → `pending_transition` cleared, investigation continues

**MULTIPLE SOLUTIONS HANDLING**:

If multiple solutions exist, the agent proposes resolution when AT LEAST ONE
solution appears effective. The user confirms which solution resolved the issue.

```python
def propose_transition(case, to_status, reason, summary, evidence_ids=None, closure_reason=None):
    """Store a pending transition proposal. Does NOT execute."""
    case.pending_transition = {
        "to_status": to_status,
        "reason": reason,
        "summary": summary,
        "evidence_ids": evidence_ids or [],
        "proposed_at": datetime.now(UTC).isoformat(),
        "proposed_by": "agent",
    }
    if closure_reason:
        case.pending_transition["closure_reason"] = closure_reason

def confirm_pending_transition(case, user_id):
    """Execute transition after user confirms.

    Raises ValueError if case is in an invalid state for the requested
    transition (e.g., trying to resolve a case that is not INVESTIGATING).
    pending_transition is only cleared after successful execution.
    """
    if pending["to_status"] == "resolved":
        _execute_resolved_transition(case, user_id, pending["reason"])
    elif pending["to_status"] == "closed":
        close_reason = pending.get("closure_reason", pending["reason"])
        _execute_closed_transition(case, user_id, close_reason)
    case.pending_transition = None
    # DISPOSITION - no further case actions
```

**Why not automatic?** The LLM's interpretation of "it works" can be wrong.
The user might mean "this command works" not "the whole system is fixed."
Disposition actions are irreversible, so false positives are costly.

**CLOSED transitions also use the handshake.** Unlike RESOLVED, CLOSED transitions
don't need readiness checks, but `assess_closure_readiness(case)` produces a
meaningful investigation summary for the confirmation prompt. This gives the user a
chance to see what was accomplished before committing to an irreversible action.

**needs_info flag for RESOLVED:** When resolution readiness returns `NEEDS_INFO` or
`SUGGEST_CLOSE`, the system stores the pending transition with `needs_info=True`.
This remembers the user's intent to resolve. On subsequent turns, the system
re-evaluates readiness automatically. When the case becomes READY, the system
overrides the LLM response with a deterministic confirmation prompt.

#### INVESTIGATING → CLOSED (Disposition)

**Trigger**: User-Agent Handshake (same pattern as RESOLVED)

Both dropdown and NLP abandonment patterns propose a pending transition with a
closure readiness summary. The user must confirm before the transition executes.

`assess_closure_readiness(case)` summarizes what was accomplished (evidence count,
hypotheses explored, milestones completed, root cause, solutions) for the confirmation
prompt. Two verdicts: `HAS_SUBSTANCE` (shows summary) or `TRIVIAL` (minimal data warning).

```python
closure = assess_closure_readiness(case)
propose_transition(
    case=case,
    to_status="closed",
    reason="User expressed abandonment intent",
    summary=closure.message,
    closure_reason="abandoned",  # "abandoned" | "escalated" | "mitigation_sufficient" | "other"
)
# User confirms → _execute_closed_transition(case, user_id, "abandoned")
```

#### INQUIRY → CLOSED (Disposition)

**Trigger**: User-Agent Handshake (same pattern as above)

```python
closure = assess_closure_readiness(case)
propose_transition(
    case=case,
    to_status="closed",
    reason="User expressed close intent from INQUIRY",
    summary=closure.message,
    closure_reason="inquiry_only",
)
# User confirms → _execute_closed_transition(case, user_id, "inquiry_only")
```

#### INQUIRY → RESOLVED (Disposition, Fast-Track)

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
    # DISPOSITION - no further case actions
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
    CaseStatus.RESOLVED: [],        # DISPOSITION - no further case actions
    CaseStatus.CLOSED: []           # DISPOSITION - no further case actions
}
```

**Case Action Diagram** (updated with Fast-Track):

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
       │                             │ Diagnosing         │   │
       │                             │ Mitigating         │   │
       │                             │ Resolving          │   │
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
       │                     │ DISPOSITION  │    ┌──────────────┐
       │                     │ With solution│    │    CLOSED    │
       │                     └──────────────┘    │              │
       │                                         │ DISPOSITION  │
       └──(inquiry-only)─────────────────────────► No solution  │
                                                 └──────────────┘
```

### 1.4 Automatic Disposition Actions

Disposition case actions are triggered automatically based on milestone completion.

```python
async def process_turn(case: Case, user_message: str) -> str:
    """
    Process one turn and update milestones.

    AUTOMATIC TRANSITIONS:
    - Checked AFTER agent response generation
    - Triggered by milestone completion (data-driven)
    - Disposition actions are irreversible
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
    # DISPOSITION CASE ACTION HANDLING (User-Agent Handshake)
    # ============================================================
    # Disposition case actions are NEVER automatic. The agent proposes
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


# Disposition case actions (all require user confirmation):
#
# INVESTIGATING → RESOLVED:
#   - Trigger: Agent proposes via ProposedTransition + user confirms
#   - Automatic: No (requires User-Agent Handshake)
#   - Disposition: Yes (irreversible)
#
# INVESTIGATING → CLOSED:
#   - Trigger: User explicit action (force_close via UI or chat)
#   - Automatic: No (requires user intent)
#   - Disposition: Yes (irreversible)
#
# INQUIRY → CLOSED:
#   - Trigger: User explicit action (close_from_inquiry)
#   - Disposition: Yes (irreversible)
#
# INQUIRY → RESOLVED:
#   - Trigger: Fast-track KB resolution + user confirmation
#   - Disposition: Yes (irreversible)


# ============================================================
# EXPLICIT USER-TRIGGERED TRANSITIONS (Non-Automatic)
# ============================================================

def force_close_investigation(case: Case, user_id: str, reason: str):
    """
    User explicitly abandons investigation without solution.

    Trigger: User action (not automatic)
    Disposition: Yes (irreversible)
    """
    if case.status != CaseStatus.INVESTIGATING:
        raise ValueError("Can only force-close from INVESTIGATING status")

    case.atomic_update(
        status=CaseStatus.CLOSED,
        closed_at=datetime.now(UTC),
        closure_reason=reason,  # "abandoned" | "escalated" | "mitigation_sufficient" | "other"
    )
    # Note: "mitigation_sufficient" is used when user closes after mitigation
    # without pursuing RCA. UI renders as "Closed - Mitigated" (distinct from abandoned).
    case.action_history.append(CaseAction(
        from_status=CaseStatus.INVESTIGATING,
        to_status=CaseStatus.CLOSED,
        triggered_at=datetime.now(UTC),
        triggered_by=user_id,
        reason=f"User force-closed: {reason}"
    ))
    # Schedule auto-summary generation (skip-if-trivial guardrail applies)
    case._pending_summary = should_generate_terminal_summary(case)
    # DISPOSITION - no further case actions


def close_from_inquiry(case: Case, user_id: str):
    """
    Close after inquiry without formal investigation.

    Trigger: User action (not automatic)
    Disposition: Yes (irreversible)
    """
    if case.status != CaseStatus.INQUIRY:
        raise ValueError("Can only close-from-inquiry when in INQUIRY status")

    case.atomic_update(
        status=CaseStatus.CLOSED,
        closed_at=datetime.now(UTC),
        closure_reason="inquiry_only",
    )
    case.action_history.append(CaseAction(
        from_status=CaseStatus.INQUIRY,
        to_status=CaseStatus.CLOSED,
        triggered_at=datetime.now(UTC),
        triggered_by=user_id,
        reason="User closed after inquiry only"
    ))
    # Schedule auto-summary generation (skip-if-trivial guardrail applies)
    case._pending_summary = should_generate_terminal_summary(case)
    # DISPOSITION - no further case actions
```

#### 1.4.1 State Update Timing

State updates occur at specific points within a turn to ensure consistency:

| Update Type | Category | When | Trigger |
|-------------|----------|------|---------|
| `proposed_problem_statement` | — | During INQUIRY turn | LLM generates from conversation |
| `problem_statement_confirmed` | — | After user confirmation | User says "Yes" or equivalent |
| `symptom_verified` | Progress milestone | After evidence processing | LLM sets in structured output when symptoms confirmed |
| `scope_assessed` | Progress milestone | After evidence processing | LLM sets in structured output when impact scope determined |
| `timeline_established` | Progress milestone | After evidence processing | LLM sets in structured output when timeline understood |
| `changes_identified` | Progress milestone | After evidence processing | LLM sets in structured output when changes correlated |
| `root_cause_identified` | Progress milestone | After hypothesis validation | LLM sets when hypothesis validated with high confidence |
| `solution_proposed` | Progress milestone | After LLM proposes action | Set when ProposedAction with action_type=SOLUTION is created |
| `path_selection` | — | When `symptom_verified` milestone completes (single trigger point) | Automatic from problem verification data. Reverted if milestone validation invalidates `symptom_verified`. |
| `mitigation_accepted` | Gate milestone | LLM structured output | LLM detects user complied with proposed temp fix (submitted results) |
| `mitigation_verified` | Gate milestone | LLM structured output | LLM detects user confirms mitigation worked → return to DIAGNOSIS |
| `solution_accepted` | Gate milestone | LLM structured output | LLM detects user complied with proposed solution (submitted results) |
| `solution_verified` | Gate milestone | After user confirms fix | User confirms problem resolved (User-Agent Handshake) |
| Disposition action | — | End of turn | After all other processing |

**Gate milestones vs Progress milestones**:

- **Gate milestones** (`mitigation_accepted`, `mitigation_verified`, `solution_accepted`, `solution_verified`): Drive stage transitions. Set by the LLM in structured output when it detects user compliance with a ProposedAction. The LLM is the compliance detector — the user's action is the trigger; the LLM recognizes it (Framework §4.1).
- **Progress milestones** (`symptom_verified`, `scope_assessed`, `timeline_established`, `changes_identified`, `root_cause_identified`, `solution_proposed`): Provide LLM context and analytics. Do NOT drive stage transitions.

**Order of Operations Within a Turn**:

1. **Receive user message**
2. **LLM processes** and generates response + `state_updates`
3. **Apply state updates**: progress milestones, gate milestones, evidence, hypotheses (all from LLM structured output)
4. **Gate milestone side effects**: When a gate milestone is set, mark the corresponding ProposedAction as accepted; stage transition takes effect next turn
5. **Record turn progress** (detect what changed)
6. **Check disposition actions** (RESOLVED/CLOSED) if conditions met
7. **Return response to user**

**Rationale**: Disposition actions happen last to ensure all state is consistent before case becomes immutable. Gate milestones are applied from the LLM's structured output alongside progress milestones; the new stage's prompt takes effect on the next turn.

### 1.5 Manual Case Action Requests

**Purpose**: Allow users to manually request case actions for practical scenarios (urgent issues, external resolutions, etc.)

**Core Principle**: Manual case actions follow the same confirmation pattern as natural progression - **all case actions require explicit user confirmation**.

---

#### 1.5.1 UI Component: Case Action Dropdown

**Location**: Case header (collapsed view)

**Behavior**:
- Shows current status with dropdown indicator
- Displays only **forward transitions** (case actions are irreversible)
- Dispositions (RESOLVED, CLOSED) have dropdown disabled

**Available Options by Status**:

| Current Status | Dropdown Options |
|---------------|------------------|
| INQUIRY       | Investigating, Closed |
| INVESTIGATING | Resolved, Closed |
| RESOLVED      | *(disabled - disposition)* |
| CLOSED        | *(disabled - disposition)* |

**API Support**: No direct API - uses existing query submission endpoint

---

#### 1.5.2 Request Flow

**Step 1: User Initiates Request**

User selects new status from dropdown → Frontend shows confirmation modal:

```
⚠️ Request Case Action

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

##### Step 3: Agent Validates and Responds

The dropdown injects a pre-composed message and routes through the normal INQUIRY
LLM pipeline. The LLM handles validation and confirmation:

**With problem statement**: The LLM presents the existing problem description for
confirmation. When the LLM sets `user_confirmed_investigation=True`, the transition
fires automatically through `_check_automatic_transitions`.

**Without problem statement**: The LLM asks the user to describe the problem.
No transition occurs until the user provides context and the LLM confirms.

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
3. **Records case action** in `action_history`
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
- `case.action_history.append(CaseAction(...))`

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

#### 1.5.4 Case Action Confirmation Examples

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

Before presenting the confirmation, the system runs `assess_resolution_readiness(case)` which checks for root cause + solution. Three outcomes:

- **READY** — Root cause and solution present. System shows what's on record and asks user to confirm.
- **NEEDS_INFO** — Partially ready (e.g., root cause but no solution). System asks user to provide the missing piece.
- **SUGGEST_CLOSE** — No root cause, no solution, no evidence. System tells user the case can't be resolved and suggests closing instead. If the issue was actually fixed, user can provide the root cause and solution.

```python
readiness = assess_resolution_readiness(case)

if readiness.verdict == "suggest_close":
    return readiness.message  # Suggests closing; offers to accept resolution info

if readiness.verdict == "needs_info":
    return readiness.message  # Asks for missing root cause or solution

# READY — show what's on record and ask for confirmation
return f"""You've indicated this issue is resolved.

Here's what I have on record:
- **Root cause**: {case.root_cause_conclusion.root_cause}
- **Solution**: {case.solutions[-1].title}

Is this correct? Once you confirm, I'll mark the case as resolved.

What will happen:
- A Resolution Summary will be auto-generated
- No further evidence submission or investigation will be possible
- You can still ask questions about this case and regenerate the summary report
- Archive the case from Dashboard when you are done

[✅ Yes, mark as resolved]  [❌ No, continue investigating]"""
```

**INVESTIGATING → CLOSED**

```python
# Agent confirms closure with consequences
return f"""You've requested to close this case without resolution.

Problem: {case.problem_verification.symptom_statement}
Current findings: {case.working_conclusion.summary if exists else "Limited data"}

Here's what will happen when I close this case:

- A Closure Summary will be auto-generated capturing investigation state and findings so far
- No further evidence submission or investigation will be possible
- You can still ask questions about this case and regenerate the summary report
- Archive the case from Dashboard when you are done

Please select a closure reason:

[Abandoned]  [Escalated]  [Mitigation Sufficient]  [Other]

Or type your reason."""
```

**INQUIRY → CLOSED**

```python
# Agent confirms inquiry-only closure with consequences
return f"""You've requested to close this case without investigation.

Here's what will happen:

- A Closure Summary will be auto-generated with the inquiry conversation
- The case will remain on your list until archived from the Dashboard

Close this case?

[✅ Yes, close]  [❌ No, keep open]"""
```

**Ambiguous "close this case" (NLP pattern)**

When a user types "close this case" during INVESTIGATING without specifying resolved or closed, the system asks for clarification. No `pending_transition` is set — we don't know the user's intent yet. Their next message routes through the standard pattern matching (resolve_patterns or abandonment_patterns).

```python
# No pending_transition set — just ask for clarification
return """You'd like to close this case. Before I do, I need to know:

- **Resolved** — The problem is fixed. I'll document the solution.
- **Closed** — The investigation is ending without a solution
  (abandoned, escalated, or mitigation was sufficient).

Which would you like?"""
```

---

#### 1.5.5 API Summary

All manual case actions use **existing endpoints** - no new APIs required:

| Action | Endpoint | Method | Body |
|--------|----------|--------|------|
| Submit case action request | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "[User requested to change case status to Investigating]"}` |
| User clicks Yes button | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "Yes"}` |
| User clicks No button | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "No"}` |
| User types qualified answer | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "<user's typed message>"}` |

**All messages appear in conversation history** - full audit trail maintained.

---

#### 1.5.6 Design Rationale

**Why dropdown menu instead of pure chat?**
- **Discoverability**: Users see available case actions
- **Clarity**: Visual indicator of current status + forward-only options
- **Efficiency**: One click vs composing message
- **Removes ambiguity**: "Let's investigate" could mean many things

**Why agent confirmation instead of direct case action?**
- **Consistency**: Same pattern as natural progression (all case actions require confirmation)
- **Safety**: Agent can validate prerequisites and catch mistakes
- **Context**: Agent ensures mutual understanding before transition
- **Audit**: Full conversation record of why the case action occurred

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

### 1.7 Post-Terminal Lifecycle

When a case reaches a disposition (RESOLVED or CLOSED), the investigation engine stops but the case remains interactive until archived. The post-terminal lifecycle defines two **interaction modes** — no new database fields required.

#### 1.7.1 Case Interaction Modes

```
┌─────────────┐   terminal    ┌──────────────┐   user        ┌──────────┐
│   ACTIVE    │──transition──►│   TERMINAL   │──archives───► │ ARCHIVED │──retention──► removed
│             │               │              │               │          │   expires
└─────────────┘               └──────────────┘               └──────────┘
 Evidence ✓                    Evidence ✗                      No interaction
 Milestones ✓                  Q&A over case data ✓            Not in default list
 Agent turns ✓                 View/download reports ✓         Reports: viewable if
 Full investigation            Regenerate summary ✓              unarchived
                               Knowledge extraction ✓
                                 (RESOLVED only)
```

**Derivation logic** (no new stored field):

```python
@property
def is_terminal(self) -> bool:
    """Case has reached a disposition (RESOLVED or CLOSED)."""
    return self.status in [CaseStatus.RESOLVED, CaseStatus.CLOSED]
```

#### 1.7.2 Terminal Mode

**Purpose**: Allow users to ask questions about the completed investigation, manage the summary report, and generate runbooks. The agent answers from existing case data only — no new investigation. The summary report can be regenerated at any time before archival.

**Behavior**:

- `_process_turn_impl()` short-circuits before intent detection and milestone processing
- Routes to **TERMINAL_TEMPLATE** prompt with `TerminalResponse` schema
- The template instructs the LLM: answer questions using existing case data, do not propose new actions or evidence requests
- Agent has read access to: messages, evidence, hypotheses, solutions, action_history, auto-generated summary
- Agent can NOT: accept new evidence, update milestones, propose transitions
- Agent CAN: explain what happened, clarify evidence, interpret timeline, extract lessons learned

**Three interaction scenarios**:

1. **User asks to regenerate the report** → Pattern matching triggers report regeneration without an LLM call. Fire-and-forget, directs user to Dashboard.
2. **User accepts runbook suggestion** (RESOLVED only) → Pattern matching triggers `_handle_runbook_creation()`: evaluates readiness + deduplication, then calls `ConversionService.convert_from_case()` as fire-and-forget background task. Directs user to Dashboard Knowledge > Drafts.
3. **User asks questions about the case** → Agent answers via the LLM with TERMINAL_TEMPLATE.

**Implementation in milestone engine**:

```python
async def _process_turn_impl(self, case, user_message, ...):
    ...
    # 0a. Terminal case handling — Q&A and report regeneration only
    if case.is_terminal:
        return await self._process_terminal_turn(case, user_message, metadata)

    # Normal investigation flow...
```

**Report regeneration**: The summary report is auto-generated at closure time (same turn). Users can request regeneration at any point while the case is in terminal state. Regeneration overwrites the existing report — there is always exactly one summary report per case.

**API-level enforcement** (`submit_turn` endpoint):

| Input                    | Terminal case behavior           |
| ------------------------ | -------------------------------- |
| Text query only          | Allowed — routed to terminal Q&A |
| Files or pasted content  | Rejected — 409 Conflict          |
| Status transition intent | Rejected — 409 Conflict          |

**Archived cases**: All interaction rejected with 409 Conflict. Archived cases are hidden from default list but remain accessible via "Include archived" filter.

#### 1.7.3 Auto-Generated Terminal Summary

When a case reaches any terminal state, the system automatically generates a lightweight summary report in the same turn. This is the canonical "what happened" record, generated using the SYNTHESIS LLM capability (cheap/fast provider).

**Two summary types**:

| Case Status | Report Type | Content Focus |
|-------------|-------------|---------------|
| RESOLVED | `RESOLUTION_SUMMARY` | What the problem was, root cause, solution applied, confirming evidence, timeline, milestones reached, investigation path used |
| CLOSED | `CLOSURE_SUMMARY` | What the problem was, investigation state at closure, approaches attempted, closure reason, leading hypotheses with confidence, mitigation status, recommendation for next investigator (if escalated) |

**Generation approach**:

- Single LLM call using SYNTHESIS capability (Fireworks/Groq for speed and cost)
- Input assembled via `context_builder.py`: case messages, evidence list, hypothesis states, action_history, milestone progress
- Stored as `Report` with `auto_generated=True` (distinguishes from user-requested reports)
- **Fire-and-forget**: failure does not block the transition. Error logged, case still transitions. Summary can be regenerated later via chat.
- One report per case — regeneration overwrites the existing report

**Resolution Summary content**:

```
Problem Statement    — One-line description of the issue
Root Cause           — What was identified as the cause
Solution Applied     — What fixed it, with key commands/configs
Confirming Evidence  — Which evidence items confirmed the fix
Timeline             — created_at → key milestones → resolved_at
Milestones Reached   — Which of the 9 progress milestones completed
Investigation Path   — MITIGATION_FIRST or ROOT_CAUSE, mitigation applied?
```

**Closure Summary content**:

```
Problem Statement         — One-line description
Investigation State       — How far diagnosis progressed (milestones reached)
Approaches Attempted      — What was tried (from action_history + action_attempts)
Closure Reason            — The reason + any user-provided context
Leading Hypotheses        — Top hypotheses at time of closure with confidence
Mitigation Status         — Whether mitigation was applied (for "mitigation_sufficient")
Timeline                  — created_at → key actions → closed_at
Recommendation            — If escalated: what the next investigator should look at first
```

**Skip-if-trivial guardrail** (`should_generate_terminal_summary()` in `terminal_transitions.py`):

Auto-summary generation is scheduled for ALL terminal transition paths. The guardrail skips generation when a case lacks meaningful content. Two independent checks must both pass:

1. **Minimum conversation depth**: At least 4 messages (enough to summarize)
2. **Investigation substance** (at least one must be true):
   - Has evidence (investigation produced data)
   - Has hypotheses (investigation produced theories)
   - Has a confirmed problem description (inquiry completed)
   - Has completed milestones (investigation made progress)

Always skipped for `closure_reason == "duplicate"` — parent case has the real content.

#### 1.7.4 Session Cleanup on Terminal Transition

When a case transitions to a terminal state, all active sessions are gracefully completed:

```python
# In terminal_transitions.py, after case status update:
active_sessions = await session_repo.get_active_sessions(case.case_id)
for session in active_sessions:
    session.complete(
        findings_summary=f"Case {case.status.value}: {closure_reason}"
    )
    await session_repo.update(session)
```

Uses the existing `InvestigationSession.complete()` method. No new session statuses needed.

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

#### Phase 3: Path-Guided Agent Behavior (INVESTIGATING Status)

**When**: After path selection, throughout DIAGNOSIS

**Purpose**: Path determines whether the agent proactively offers mitigation during DIAGNOSIS

**Behavior**: The path is **advisory, not structural** — it influences what the agent proposes, not which milestones are available.

```python
def apply_path_guidance(case: Case):
    """
    Path guides agent behavior during DIAGNOSIS.

    For MITIGATION_FIRST: Agent proactively offers temp fix during DIAGNOSIS.
        Actual entry to MITIGATION stage is inferred from user compliance.
    For ROOT_CAUSE: Agent proceeds with root cause analysis. Mitigation not
        offered unless user requests it.
    For USER_CHOICE: Agent presents both options and lets user decide.
    """
    if case.path_selection.path == InvestigationPath.MITIGATION_FIRST:
        # Agent prompt includes urgency context and mitigation guidance.
        # Agent proposes a concrete temp fix action during DIAGNOSIS.
        # If user complies (executes and submits results) → system infers
        # DIAGNOSIS → MITIGATION transition via mitigation_accepted milestone.
        pass
    elif case.path_selection.path == InvestigationPath.ROOT_CAUSE:
        # Agent proceeds directly to root cause analysis in DIAGNOSIS.
        # No mitigation offered unless user explicitly requests it.
        pass
    elif case.path_selection.path == InvestigationPath.USER_CHOICE:
        # Agent presents both options: "Should I focus on a quick fix first,
        # or go straight to finding the root cause?"
        pass
```

**Timeline Diagram**:

```
Turn 1 (INQUIRY):     preliminary_urgency assessed → Early hint provided
Turn 2 (INQUIRY→INVESTIGATING): Case action → enters DIAGNOSIS stage
Turn 3 (INVESTIGATING/DIAGNOSIS): symptom_verified set → path_selection determined → agent behavior guided by path
Turn N (INVESTIGATING/DIAGNOSIS): Agent proposes action → user complies → inferred transition to MITIGATION or TREATMENT
```

### 2.1 Path Selection Matrix

Based on **temporal_state × urgency_level**:

| Temporal State | Urgency | Path | Rationale |
|----------------|---------|------|-----------|
| **Ongoing** | CRITICAL | MITIGATION_FIRST (auto) | Production broken NOW - stop impact, RCA later |
| **Ongoing** | HIGH | MITIGATION_FIRST (auto) | Significant active impact - stop bleeding first |
| **Ongoing** | MEDIUM | USER_CHOICE | User decides: quick mitigation or thorough RCA |
| **Ongoing** | LOW | USER_CHOICE | Minor issue, user decides approach |
| **Historical** | CRITICAL | USER_CHOICE | Clarify why critical if past issue — user knows whether speed or thoroughness matters more |
| **Historical** | HIGH | USER_CHOICE | Past issue with high urgency — user decides mitigation-first or RCA |
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

    # AUTO: Historical + Low/Medium/High Urgency → ROOT_CAUSE (permanent solution)
    if temporal == TemporalState.HISTORICAL and urgency in [UrgencyLevel.LOW, UrgencyLevel.MEDIUM, UrgencyLevel.HIGH]:
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

The path determines **whether the agent proactively offers mitigation** during DIAGNOSIS. Both paths use the same 2-stage model with mitigation detour (DIAGNOSIS → TREATMENT, with optional MITIGATION detour), but differ in agent behavior.

---

**Path (a): MITIGATION_FIRST**

MITIGATION is a **distinct stage** — a controlled detour to stabilize the situation before root cause analysis.

- **DIAGNOSIS** (initial)
  - Agent detects urgency from problem verification
  - Agent proposes a concrete temp fix action (e.g., "Run `kubectl rollout undo deployment/payment-api`")
  - If user complies (executes and submits results) → **inferred transition to MITIGATION**
  - If user questions or refuses → stays in DIAGNOSIS, agent refines approach

- **MITIGATION** (stabilize)
  - Agent verifies whether the temp fix worked (asks for metrics/logs)
  - If mitigation insufficient → agent adjusts approach, iterates within MITIGATION
  - Once user verifies mitigation is effective → **return to DIAGNOSIS** for root cause analysis
  - The system always directs toward RCA. The user can manually close via UI (→ CLOSED with `closure_reason="mitigation_sufficient"`), but the system does not offer a "mitigation-only resolution" flow path.

- **DIAGNOSIS** (resumed)
  - Agent resumes root cause analysis with reduced pressure (service stable)
  - Forms hypotheses, tests against evidence, identifies root cause
  - Proposes permanent solution action
  - If user complies → **inferred transition to TREATMENT**

- **TREATMENT**
  - Agent verifies fix worked
  - If fix failed → extended diagnosis within TREATMENT (failure analysis → new evidence → new hypothesis → revised fix)
  - If fix worked → user confirms via User-Agent Handshake → **RESOLVED**

**Stage flow**: DIAGNOSIS → MITIGATION → DIAGNOSIS → TREATMENT → RESOLVED

**Gate milestones**: `mitigation_accepted` → `mitigation_verified` → `solution_accepted` → `solution_verified`

**Progress milestones** (non-driving): `symptom_verified`, `scope_assessed`, `timeline_established`, `changes_identified`, `root_cause_identified`, `solution_proposed`

---

**Path (b): ROOT_CAUSE**

Direct root cause analysis — no mitigation detour.

- **DIAGNOSIS**
  - Verify symptoms, scope, timeline (no active impact or low urgency)
  - Form hypotheses, test against evidence
  - Identify root cause with sufficient confidence
  - Propose permanent solution action
  - If user complies → **inferred transition to TREATMENT**

- **TREATMENT**
  - Agent verifies fix worked
  - If fix failed → extended diagnosis within TREATMENT
  - If fix worked → user confirms via User-Agent Handshake → **RESOLVED**

**Stage flow**: DIAGNOSIS → TREATMENT → RESOLVED

**Gate milestones**: `solution_accepted` → `solution_verified`

**Progress milestones** (non-driving): `symptom_verified`, `scope_assessed`, `timeline_established`, `changes_identified`, `root_cause_identified`, `solution_proposed`

---

**Key Differences**:

| Aspect | MITIGATION_FIRST | ROOT_CAUSE |
|--------|------------------|------------|
| Stage flow | DIAGNOSIS → MITIGATION → DIAGNOSIS → TREATMENT | DIAGNOSIS → TREATMENT |
| Mitigation | Agent proactively offers temp fix in DIAGNOSIS | Not offered unless user requests |
| Stage transitions | Inference-based (user compliance) | Inference-based (user compliance) |
| Pressure | Reduced early via MITIGATION detour | Full pressure until resolution |
| Use case | ONGOING + HIGH/CRITICAL | HISTORICAL + LOW/MEDIUM/HIGH |

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

# Minimum evidence expectations for PROGRESS MILESTONES (non-stage-driving):
# These are validated when the LLM claims a progress milestone has been reached.
PROGRESS_MILESTONE_EVIDENCE_EXPECTATIONS = {
    "symptom_verified":     {"min_evidence": 1, "categories": [SYMPTOM_EVIDENCE]},
    "scope_assessed":       {"min_evidence": 1, "categories": [SYMPTOM_EVIDENCE]},
    "timeline_established": {"min_evidence": 1, "categories": [SYMPTOM_EVIDENCE]},
    "changes_identified":   {"min_evidence": 1, "categories": [SYMPTOM_EVIDENCE, CAUSAL_EVIDENCE]},
    "root_cause_identified":{"min_evidence": 2, "categories": [CAUSAL_EVIDENCE]},
    "solution_proposed":    {"min_evidence": 0, "categories": []},  # Set programmatically when
                                                                     # ProposedAction with action_type=SOLUTION is created
}

# GATE MILESTONES are NOT evidence-validated — they are set by
# the LLM in structured output when it detects user compliance (Framework §4.2):
#   - mitigation_accepted: User complied with proposed temp fix
#   - mitigation_verified: User confirmed mitigation worked
#   - solution_accepted:   User complied with proposed solution
#   - solution_verified:   User confirmed fix worked (User-Agent Handshake)
```

**Evidence Classification**:

Evidence is created after LLM evaluation with a specific category assigned.
See [Evidence Classification Design](../data-processing/evidence-classification-design.md) for complete details.

| Category | Description | Used In Stage |
|----------|-------------|---------------|
| `SYMPTOM_EVIDENCE` | Data showing the problem exists (verifies symptoms, scope, timeline, changes) | DIAGNOSIS, TREATMENT |
| `CAUSAL_EVIDENCE` | Data explaining why the problem happened (requires hypothesis to exist) | DIAGNOSIS, TREATMENT |
| `MITIGATION_EVIDENCE` | Data showing whether the temporary fix worked | MITIGATION |
| `SOLUTION_EVIDENCE` | Data showing whether the permanent fix worked | TREATMENT |
| `CONTEXTUAL_EVIDENCE` | Provides baseline/environmental context | DIAGNOSIS, TREATMENT |
| `REJECTED` | Analyzed but not useful for investigation | Any |

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

    # Detect state changes (both gate milestones and progress milestones)
    STAGE_GATE_MILESTONES = {"mitigation_accepted", "mitigation_verified", "solution_accepted", "solution_verified"}
    PROGRESS_INDICATORS = {"symptom_verified", "scope_assessed", "timeline_established",
                           "changes_identified", "root_cause_identified", "solution_proposed"}

    all_changed = [
        key for key in progress_before
        if isinstance(progress_before[key], bool)
        and progress_before[key] == False
        and progress_after[key] == True
    ]

    # Gate milestone changes trigger stage recomputation
    stage_gate_completed = [k for k in all_changed if k in STAGE_GATE_MILESTONES]

    # Progress milestone changes are recorded but do NOT affect stage
    indicators_completed = [k for k in all_changed if k in PROGRESS_INDICATORS]

    milestones_completed = all_changed  # Both types are recorded in turn history

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

    # Determine if progress made (broadened definition)
    progress_made = _check_if_progress_made(metadata)

    # ============================================================
    # PROGRESS DEFINITION (for turns_without_progress counter)
    # ============================================================
    #
    # Progress IS made when ANY of the following occur:
    #
    # STRUCTURAL ARTIFACTS:
    # - Gate milestone transitions False → True (e.g., solution_accepted)
    # - Progress milestone transitions False → True (e.g., symptom_verified)
    # - Evidence is added to the case
    # - New hypothesis is generated
    # - Hypothesis status changes (ACTIVE → VALIDATED/REFUTED)
    # - ProposedAction is created (agent proposed something actionable)
    # - User confirms problem statement or path selection
    # - Files uploaded
    # - Case action occurred (phase transition or disposition change)
    #
    # INVESTIGATIVE BEHAVIORS (a skilled troubleshooter gathering data IS progressing):
    # - TurnOutcome.DATA_REQUESTED — agent asking for specific data
    # - TurnOutcome.HYPOTHESIS_TESTED — hypothesis evaluated this turn
    # - TurnOutcome.DATA_PROVIDED — user responded with requested data
    # - hypothesis_evidence_links_applied > 0 — evidence linked to hypotheses
    #
    # Progress is NOT made when:
    # - Pure CONVERSATION with no structural or investigative activity
    # - Agent repeats previous information
    # - Conversation is off-topic or circular
    #
    # RATIONALE: The old definition only counted structural artifacts, causing
    # premature stagnation detection when the agent was actively investigating
    # (requesting data, testing hypotheses, linking evidence). A copilot that
    # is actively gathering information should not be penalized.

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

    # Stagnation detection (threshold: 5 turns)
    # When turns_without_progress exceeds threshold, the stagnation breaker
    # emits a gentle_reminder BreakoutAction — a patient prompt injection that
    # nudges the LLM toward the next diagnostic step without lowering confidence
    # or suggesting escalation. FaultMaven is a copilot; the user decides the pace.
    # This is a prompt hint, not a mode change — the agent continues doing
    # the same thing it always does (analyzing data, surfacing insights, guiding).

    return turn


def determine_turn_outcome(case: Case, progress_made: bool) -> TurnOutcome:
    """
    Determine turn outcome classification.

    Checked AFTER milestone detection and evidence processing.
    Used for LLM observability and metrics (not workflow control).
    """

    # Disposition action
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
- Mitigation proposals (during DIAGNOSIS, primarily on MITIGATION_FIRST path but applicable whenever agent detects urgency)
- Hypothesis generation (all paths, in DIAGNOSIS and extended diagnosis within TREATMENT)
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
**Flow**: `INQUIRY` → `INVESTIGATING` (DIAGNOSIS → TREATMENT) → `RESOLVED`

#### Workflow Steps & Milestones

**Phase 1: Inquiry**
*   **Goal**: Establish problem statement.
*   **Transition Trigger**: User confirms problem statement and decides to investigate.

**Phase 2: Investigation**

*   **DIAGNOSIS Stage** (natural flow, not sequential sub-stages)
    *   Agent verifies symptoms, scope, timeline using evidence
    *   Progress milestones set by LLM: `symptom_verified`, `scope_assessed`, `timeline_established`, `changes_identified`
    *   Agent forms hypotheses, tests against evidence
    *   Progress milestone: `root_cause_identified` (when hypothesis validated with high confidence)
    *   Agent proposes concrete solution action
    *   Progress milestone: `solution_proposed` (when ProposedAction with action_type=SOLUTION created)
    *   **Constraint**: A hypothesis must exist before evidence can be classified as `causal_evidence`

*   **DIAGNOSIS → TREATMENT transition** (inference-based)
    *   User complies with proposed solution (executes and submits results)
    *   System infers acceptance → gate milestone: `solution_accepted`
    *   If user questions or refuses → stays in DIAGNOSIS, agent refines approach

*   **TREATMENT Stage** (iterative resolution)
    *   Agent verifies whether fix worked from submitted evidence
    *   If fix worked → agent proposes resolution via User-Agent Handshake
    *   If fix failed → extended diagnosis within TREATMENT:
        *   Failure analysis → gap identification → targeted evidence request → new hypothesis → revised fix
        *   New evidence required (the original evidence produced a failed solution)
        *   Escalation when no viable options remain (agent communicates limitations naturally)

**Phase 3: Resolution**
*   **Transition Trigger**: User confirms fix worked via User-Agent Handshake → gate milestone: `solution_verified`
*   **State**: `RESOLVED`.

---

### 4.4 Mitigation-First Investigation (Ongoing Outage)
**User Goal**: Restore service availability immediately.
**Trigger**: High Severity + Ongoing Outage (auto-selected or user-chosen path).

After mitigation is verified, the system always directs toward root cause analysis.
The user can manually close the case via UI at any point, but the system does not
offer a "mitigation-only resolution" flow path.

#### Full Path (Mitigation + RCA → RESOLVED)
**Flow**: `INQUIRY` → `INVESTIGATING` (DIAGNOSIS → MITIGATION → DIAGNOSIS → TREATMENT) → `RESOLVED`

**Gate milestones**:
*   `mitigation_accepted`: User complied with proposed temp fix (inferred from submission).
*   `mitigation_verified`: Mitigation verified effective → return to DIAGNOSIS.
*   `solution_accepted`: User complied with proposed permanent solution (inferred from submission).
*   `solution_verified`: Permanent fix validated (via User-Agent Handshake).

**Progress milestones** (non-driving):
*   `symptom_verified`, `scope_assessed`, `timeline_established`, `changes_identified`: Set during DIAGNOSIS.
*   `root_cause_identified`: Set when hypothesis validated with high confidence.
*   `solution_proposed`: Set when ProposedAction with action_type=SOLUTION created.

#### Mitigation-Only Closure (User Override → CLOSED)
**Flow**: `INQUIRY` → `INVESTIGATING` (DIAGNOSIS → MITIGATION → DIAGNOSIS) → `CLOSED`

The user decides the mitigation is sufficient and does not want RCA. This is a
**user-initiated closure**, not a system-offered path. The system always returns
to DIAGNOSIS after mitigation; the user closes via UI.

**Gate milestones**:
*   `mitigation_accepted`: User complied with proposed temp fix.
*   `mitigation_verified`: Mitigation verified effective → return to DIAGNOSIS.
*   `solution_accepted`: **Not set** (user closed before proposing permanent solution).
*   `solution_verified`: **Not set** (no permanent fix).

**Closure**: `CaseStatus.CLOSED` with `closure_reason="mitigation_sufficient"`.
UI renders as "Closed - Mitigated" (distinct from "Closed - Abandoned").

#### Agent Behavior After Mitigation

After `mitigation_verified` is set, the system returns to DIAGNOSIS for root cause
analysis. The agent resumes investigation:

> "The mitigation is working — [specific metric showing improvement]. Now let's
> investigate the root cause to prevent recurrence. What additional data can you
> share about what changed before this started?"

The system pushes toward RCA. The user can always close via UI if they decide
the mitigation is sufficient, but the agent does not offer closure as an option.

#### MITIGATION Is Iterative

Mitigation is not assumed to be one-shot. Within the MITIGATION stage, the agent
may adjust its approach and propose multiple temp fix attempts until the user
verifies stabilization.

**Reset mechanism**: When `mitigation_verified` is completed as a gate
milestone, `_apply_stage_gate_side_effects()` (in `milestone_engine.py`) resets
both `mitigation_accepted` and `mitigation_verified` to `False`. This happens
as a side effect of the same function that marks the corresponding
`ProposedAction` as "accepted" and creates an `ActionAttempt` audit record.
The completed mitigation is preserved in the `action_attempts` list. The reset
allows a new MITIGATION detour if a future urgent situation arises.

#### How the System Distinguishes Outcomes (Retrospectively)

The boolean milestone flags reflect the **current** cycle, not history.
After the mitigation flag reset, `mitigation_accepted` and `mitigation_verified`
are both `False`. To determine whether mitigation occurred, query the
`action_attempts` list for entries with `action_type=MITIGATION`.

| Field | Full Path (RESOLVED) | Mitigation-Only (CLOSED) | No Mitigation (RESOLVED) |
| ----- | ------------------- | ------------------------ | ------------------------ |
| `mitigation_accepted` | False (reset) | False (reset) | False |
| `mitigation_verified` | False (reset) | False (reset) | False |
| `solution_accepted` | True | False | True |
| `solution_verified` | True | False | True |
| `root_cause_identified` | True | May be partial | True |
| `CaseStatus` | RESOLVED | CLOSED | RESOLVED |
| `closure_reason` | "resolved" | "mitigation_sufficient" | "resolved" |
| `action_attempts` has MITIGATION | Yes | Yes | No |

The combination of `CaseStatus`, `closure_reason`, and `action_attempts` history
provides the full classification. Analytics should query `action_attempts` to
determine mitigation involvement, not the boolean flags.

---

### 4.5 Post-Terminal Operations

After a case reaches RESOLVED or CLOSED, the system auto-generates a terminal summary. For resolved cases, the user may also request runbook generation.

#### 4.5.0 Auto-Generated Terminal Summary

**Trigger**: Automatic on terminal transition (both RESOLVED and CLOSED), fire-and-forget — failure does not block the transition.

**Implementation**: `MilestoneEngine._auto_generate_report()` calls `ReportGenerationService.generate_reports()` after the case is saved in terminal state. Called from both transition paths in the milestone engine (dropdown confirm and main process_turn).

**Guardrail**: `should_generate_terminal_summary()` in `terminal_transitions.py` skips generation for:
- Duplicate closures (`closure_reason == "duplicate"`) — parent case has the real content
- Trivial cases — no evidence AND no hypotheses AND fewer than 4 messages

**Summary types**:

| Case Status | Report Type | Content Structure |
|-------------|-------------|-------------------|
| RESOLVED | `RESOLUTION_SUMMARY` | Problem Statement, Root Cause (from validated hypotheses), Solution Applied, Confirming Evidence, Timeline, Milestones Reached, Investigation Path |
| CLOSED | `CLOSURE_SUMMARY` | Problem Statement, Investigation State (milestones/evidence/hypotheses counts), Closure Reason, Leading Hypotheses (top 5 by confidence), Mitigation Status, Timeline, Recommendation (for escalated/abandoned cases) |

Summaries are built from case data fields (hypotheses, solutions, evidence, milestones, timestamps). Stored as `CaseReport` records with `auto_generated=True`. Duration is calculated from `created_at` to `resolved_at` or `closed_at`.

**Report type enum** (`ReportType` in `case/domain/owned_models/report.py`):
- `RESOLUTION_SUMMARY` — auto-generated for resolved cases
- `CLOSURE_SUMMARY` — auto-generated for closed cases
- `RUNBOOK` — user-requested via ConversionService (see §4.5.2)

**Dashboard**: `ReportTab` is view-only — displays auto-generated summaries with formatted markdown rendering and download. No manual generate button. If no summary was generated (trivial case), the tab explains why.

**API endpoints:**

- `GET /api/v1/cases/{case_id}/reports` — List generated reports
- `GET /api/v1/cases/{case_id}/reports/{report_id}/download` — Download report
- `POST /api/v1/cases/{case_id}/reports` — Regenerate (requires terminal state)

#### 4.5.2 Runbook Generation (Knowledge Flywheel)

**Eligibility**: RESOLVED cases only. CLOSED cases are not eligible — quality over quantity.

**Design**: Suggest first, evaluate on acceptance. The agent always offers a COOPERATIVE suggestion at resolution time. Readiness assessment and deduplication happen only when the user accepts — not upfront. This avoids wasted computation and gives the user a clear accept/decline choice.

**Trigger flow (Copilot)**:

```text
User confirms resolution
    → Agent offers COOPERATIVE suggestion: "Would you like me to create a runbook?"
    → User accepts
        → System evaluates readiness + deduplication
        → Four possible outcomes:
            SUCCESS           → Draft created, user redirected to Dashboard
            NOT_SUITABLE      → "Not enough data for a quality runbook" (no draft)
            EXISTING_COVERS   → "Similar runbook exists: {title} ({score}% match)"
            GENERATION_FAILED → "Generation failed, try again later"
    → User declines or ignores
        → No evaluation, no side effects
```

**Trigger flow (Dashboard)**:

Users can also generate runbooks from the Dashboard RunbookTab on resolved cases. The same readiness + dedup evaluation applies when the user clicks "Generate Runbook".

**Readiness assessment** (`assess_runbook_readiness()` in `terminal_transitions.py`):

Maps case data to the 7 canonical runbook sections and checks coverage.

| Verdict | Condition | Outcome |
|---------|-----------|---------|
| `READY` | Problem + root cause + actionable solution + at most 1 enrichment gap | Draft generated |
| `NEEDS_ENRICHMENT` | Critical sections OK, but 2+ enrichment sections thin | Draft generated with quality warning |
| `NOT_SUITABLE` | Missing problem definition or root cause with actionable fix | `NOT_SUITABLE` outcome — no draft |

**Deduplication** (`RunbookKnowledgeBase` vector search):

| Similarity | Verdict | Outcome |
|------------|---------|---------|
| ≥85% | `EXISTING_COVERS` | No new draft — link to existing runbook |
| 70-84% | `SUGGEST_WITH_CAVEATS` | Draft generated with note about similar runbook |
| <70% | No conflict | Draft generated normally |

**Workflow** (canonical path via `ConversionService`, triggered after user accepts):

1. `POST /api/v1/knowledge/convert-from-case` — extracts case data (solutions, root cause, hypotheses, evidence, domain/service)
2. LLM generates canonical runbook (YAML frontmatter + 7 markdown sections) using `CONVERSION_SYSTEM_PROMPT`
3. `RunbookValidator` checks structure; `QualityScorer` evaluates completeness, clarity, actionability (0-100 score)
4. Draft created in `draft` status for user review
5. User edits draft → re-validates → verifies → ingests into ChromaDB vector DB
6. Verified runbook is chunked (512 tokens, 50-token overlap), embedded (BGE-M3, 1024 dims), indexed for future similarity search

**Canonical runbook sections**: Problem Definition, Diagnostic Steps, Mitigation, Root Cause Resolution, Verification, Prevention, Sources.

**API endpoints:**

- `POST /api/v1/knowledge/convert-from-case` — Generate runbook from resolved case
- `PUT /api/v1/knowledge/conversions/{id}/drafts/{draft_id}` — Edit draft (re-validates)
- `POST /api/v1/knowledge/conversions/{id}/drafts/{draft_id}/verify` — Verify → ingest into vector DB
- `DELETE /api/v1/knowledge/conversions/{id}/drafts/{draft_id}` — Soft delete draft

#### 4.5.3 Knowledge Suggestion Extraction

**Eligibility**: RESOLVED cases only. This is a separate workflow from runbook generation — it produces structured knowledge articles (Problem, Root Cause, Solution, Prevention) rather than step-by-step runbooks.

**Trigger point**: Dashboard only (`KnowledgeTab` on CaseDetailPage).

**Workflow:**

1. User clicks "Extract Knowledge" → `POST /api/v1/knowledge/suggestions/extract`
2. LLM extracts structured article with automatic PII removal
3. Suggestion created in `PENDING_REVIEW` status with PII scan
4. Admin reviews: edit title/content, verify PII scan, approve or reject
5. On approval: creates `KnowledgeItem` in the knowledge base

**PII scan pipeline**: `NOT_SCANNED` → `SCANNING` → `CLEAN` | `PII_DETECTED` → `REMEDIATED`

**API endpoints:**

- `POST /api/v1/knowledge/suggestions/extract` — Extract from case
- `GET /api/v1/knowledge/suggestions?case_id={id}` — Get suggestion for case
- `PUT /api/v1/knowledge/suggestions/{id}` — Update title/content
- `POST /api/v1/knowledge/suggestions/{id}/approve` — Approve → create KnowledgeItem
- `POST /api/v1/knowledge/suggestions/{id}/reject` — Reject with reason
- `POST /api/v1/knowledge/suggestions/{id}/remediate-pii` — Auto-remediate PII

#### 4.5.3 Cross-Frontend Linking

The copilot links to dashboard for operations that require richer UI:

| Copilot Action                                | Dashboard URL                                      |
|-----------------------------------------------|----------------------------------------------------|
| "View in Dashboard" (after report generated)  | `{DASHBOARD_URL}/cases/{caseId}?tab=report`        |
| "Extract as knowledge article" nudge          | `{DASHBOARD_URL}/cases/{caseId}?tab=knowledge`     |

Dashboard `CaseTabs` reads the `tab` query parameter to auto-select the correct tab on load.

#### 4.5.4 Archival

Independent of post-terminal operations. User can archive any terminal case via the Dashboard case detail page. Archived cases are hidden from the default list but remain accessible via "Include archived" filter.

---

### 4.6 Abandoned / Escalated Investigation
**User Goal**: Investigation stalled or handed off to human expert.
**Flow**: `INQUIRY` → `INVESTIGATING` → `CLOSED`

#### Workflow Steps
1.  **Investigation Starts**: Gate milestones and progress milestones partially set.
2.  **Stall/Escalation**:
    *   Agent cannot find root cause (no viable options — communicates limitations and suggests escalation).
    *   User stops responding.
    *   User explicitly requests escalation.
    *   User closes after mitigation without pursuing RCA (`closure_reason="mitigation_sufficient"`, UI renders as "Closed - Mitigated").
3.  **Closure**: Case marked `CLOSED` with reason (e.g., `escalated`, `abandoned`, `mitigation_sufficient`).

#### Milestones
*   Partial completion of progress milestones (symptom_verified, scope_assessed, etc.).
*   Gate milestones may be partially set (e.g., mitigation_accepted/verified if mitigation was performed).
*   `working_conclusion`: Summary of findings up to the point of closure.
*   `action_attempts`: Complete record of all mitigation and solution actions attempted.
