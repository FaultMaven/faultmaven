# Opportunistic Investigation Framework

## Executive Summary

This document defines FaultMaven's investigation architecture using an **opportunistic approach** where the agent completes tasks based on data availability rather than following rigid sequential phases.

**Core Principles**:
- Investigation progress tracked via **milestone completions**, not phase transitions
- Agent can complete **multiple milestones in one turn** when sufficient data is available
- **Case status** (INQUIRY/INVESTIGATING/RESOLVED/CLOSED) provides user-facing lifecycle state
- **Investigation stages** (Understanding/Diagnosing/Resolving) provide optional progress detail
- Hypothesis testing is **optional exploration**, not a required workflow step

---

## Document Structure

This specification is organized into focused documents:

| Document | Contents |
|----------|----------|
| **[Investigation Data Models](./investigation-data-models.md)** | Core data structures: CaseStatus, InvestigationProgress, Evidence, Hypothesis, Solution, DegradedMode |
| **[Investigation Lifecycle Logic](./investigation-lifecycle-logic.md)** | State transitions, path routing (MITIGATION_FIRST vs ROOT_CAUSE), turn tracking |
| **[Prompt Engineering Guide](./prompt-engineering-guide.md)** | LLM prompt templates and interaction patterns |
| **[Prompt Templates](./prompt-templates.md)** | Implementation-ready prompt templates |
| **[Prompt Implementation Examples](./prompt-implementation-examples.md)** | Complete integration code examples |

---

## 1. Architectural Philosophy

### 1.1 Core Concept

Investigation is **data-driven and opportunistic**, not phase-constrained.

**The Agent**:
- Checks what data is available
- Completes all tasks for which sufficient data exists
- Records which milestones were completed
- Proceeds naturally without artificial barriers

**Example**:
```
User uploads comprehensive log file containing:
  - Error messages (symptom data)
  - Timestamps (timeline data)
  - Stack trace (root cause data)

Agent in ONE turn:
  ✅ Verifies symptom
  ✅ Establishes timeline
  ✅ Identifies root cause
  ✅ Proposes solution

No sequential phase transitions required.
```

### 1.2 Key Design Decisions

**1. Milestones Track Completion, Not Position**

```python
# Opportunistic approach: Check data availability and completion status
if has_diagnostic_data(case) and not case.progress.root_cause_identified:
    identify_root_cause()
    case.progress.root_cause_identified = True
```

**Key Insight**: Instead of tracking "what phase am I in?", the system checks "what data is available?" and "what's been completed?" This enables opportunistic task completion.

**2. Status is User-Facing Lifecycle State**

Case status answers: **"Is my problem fixed?"**
- INQUIRY: Exploring
- INVESTIGATING: Working on it
- RESOLVED: Fixed (closed WITH solution)
- CLOSED: Done (closed WITHOUT solution)

**3. Stages are Optional Progress Detail**

Investigation stage answers: **"What's the agent doing right now?"**
- Understanding: Verifying the problem
- Diagnosing: Finding root cause
- Resolving: Applying solution

**Note**: The 4-stage internal execution layer (SYMPTOM_VERIFICATION → HYPOTHESIS_FORMULATION → HYPOTHESIS_VALIDATION → SOLUTION) maps to the 3-stage user-facing layer. This is intentional state abstraction - users don't need to know the micro-step the agent is on.

**4. Hypotheses are Optional Exploration Paths (Single-Shot Validation)**

Agent may:
- Identify root cause directly from evidence using **Single-Shot Validation**
- OR generate hypotheses for systematic exploration (when cause unclear)

**Single-Shot Validation Pattern** (preserves audit trail):
When root cause is obvious, agent completes ALL of these in ONE turn:
1. CREATE hypothesis with statement = identified root cause
2. LINK evidence with stance = SUPPORTS
3. SET hypothesis status = VALIDATED
4. SET root_cause_identified = True

This preserves the full audit trail (Evidence → Hypothesis → Resolution) while
achieving the same speed as skipping hypothesis generation.

**5. Knowledge Pre-Check Enables Fast-Track Resolution**

Before formal investigation, agent searches knowledge base for:
- Similar past cases (pattern matching)
- Relevant runbooks
- Known issues for affected services

If high-confidence match found (>70%), agent offers known solution first.
If user confirms it works → Case goes directly from INQUIRY to RESOLVED (Fast-Track)

---

## 2. UI/UX Design

### 2.1 Primary Display: Case Status

```python
def render_case_header(case: Case) -> str:
    """Primary UI shows STATUS"""

    status_display = {
        CaseStatus.INQUIRY: {
            "label": "💬 Exploring",
            "description": "Discussing the problem",
            "color": "blue"
        },
        CaseStatus.INVESTIGATING: {
            "label": "🔍 Investigating",
            "description": "Working on finding and fixing the issue",
            "color": "yellow"
        },
        CaseStatus.RESOLVED: {
            "label": "✅ Resolved",
            "description": "Problem fixed (closed with solution)",
            "color": "green"
        },
        CaseStatus.CLOSED: {
            "label": "📦 Closed",
            "description": f"Closed without solution ({case.closure_reason})",
            "color": "gray"
        }
    }

    info = status_display[case.status]

    # Stage as secondary detail (only for INVESTIGATING)
    stage_detail = ""
    if case.status == CaseStatus.INVESTIGATING and case.current_stage:
        stage_labels = {
            InvestigationStage.UNDERSTANDING: "Understanding the problem",
            InvestigationStage.DIAGNOSING: "Diagnosing the cause",
            InvestigationStage.RESOLVING: "Applying solution",
        }
        stage_detail = f"\n  {stage_labels[case.current_stage]}"

    return f"""
┌─────────────────────────────────────────────┐
│ Status: {info['label']}                     │
│ {info['description']}                       │
{stage_detail}
│                                             │
│ Turn {case.current_turn} | {format_time_ago(case.updated_at)}
└─────────────────────────────────────────────┘
"""
```

---

## 3. Complete Examples

### 3.1 One-Turn Resolution

```python
user_message = """
My API is timing out. Attached error.log showing NullPointerException
at line 42 starting at 14:23 UTC. We deployed v2.1.3 at 14:20 UTC.
"""

def agent_turn_1(case: Case):
    # Complete MULTIPLE milestones in one turn
    case.progress.symptom_verified = True
    case.progress.scope_assessed = True
    case.progress.timeline_established = True
    case.progress.changes_identified = True
    case.progress.root_cause_identified = True
    case.progress.root_cause_confidence = 0.95
    case.progress.solution_proposed = True

    return """
**Investigation Complete** (1 turn)

✅ Symptom: NullPointerException causing API timeouts
✅ Timeline: Started 14:23 UTC (3 min after v2.1.3 deploy)
✅ Root cause: Missing null check at line 42 in UserService.java

**Recommended Solutions**:
1. IMMEDIATE: Rollback to v2.1.2
2. LONG-TERM: Add null check at line 42

Would you like to proceed with rollback?
"""
```

### 3.2 Status Lifecycle Example

```python
# Turn 1: Inquiry
case.status = CaseStatus.INQUIRY

# Turn 3: User decides to investigate
case.status = CaseStatus.INVESTIGATING  # Automatic transition

# Turns 4-15: Investigation
case.progress.symptom_verified = True
case.progress.root_cause_identified = True
case.progress.solution_applied = True

# Turn 17: Solution verified
case.progress.solution_verified = True
case.status = CaseStatus.RESOLVED  # Automatic transition to TERMINAL
case.resolved_at = datetime.now()
case.closed_at = datetime.now()
case.closure_reason = "resolved"

# Case is now TERMINAL - no further processing
```

### 3.3 Closed Without Solution

```python
# User abandons investigation
user: "Need to escalate to senior engineer. Close this case."

case.status = CaseStatus.CLOSED  # Transition to TERMINAL
case.closed_at = datetime.now()
case.closure_reason = "escalated"

# Case is now TERMINAL - no further processing
```

---

## Quick Reference

### Case Statuses

| Status | Description | Terminal? |
|--------|-------------|-----------|
| `INQUIRY` | Pre-investigation exploration | No |
| `INVESTIGATING` | Active formal investigation | No |
| `RESOLVED` | Closed WITH solution | Yes |
| `CLOSED` | Closed WITHOUT solution | Yes |

### Resolution Paths

| Path | Description | Transitions |
|------|-------------|-------------|
| **Standard** | Full investigation | INQUIRY → INVESTIGATING → RESOLVED |
| **Fast-Track** | Known issue from KB | INQUIRY → RESOLVED (skips INVESTIGATING) |
| **Mitigation + RCA** | Stop bleeding, then find root cause | INQUIRY → INVESTIGATING (Mitigation → RCA) → RESOLVED |
| **Mitigation Only** | Stop bleeding, user satisfied | INQUIRY → INVESTIGATING (Mitigation) → RESOLVED |
| **Abandoned** | No solution found | INQUIRY/INVESTIGATING → CLOSED |

### Investigation Paths

| Path | Mitigation | Use When |
|------|------------|----------|
| `MITIGATION_FIRST` | Applied during stages 1-2; user then chooses RCA or close | ONGOING + HIGH/CRITICAL urgency |
| `ROOT_CAUSE` | After RCA complete | HISTORICAL + LOW/MEDIUM urgency |
| `USER_CHOICE` | User decides | Ambiguous cases |

### Milestones

| Category | Milestones |
|----------|------------|
| Verification | `symptom_verified`, `scope_assessed`, `timeline_established`, `changes_identified` |
| Investigation | `root_cause_identified` |
| Resolution | `solution_proposed`, `solution_applied`, `solution_verified` |
| Mitigation | `mitigation_applied` (opportunistic, during stages 1-2) |

### Evidence Stances

| Stance | Meaning | Use With |
|--------|---------|----------|
| `SUPPORTS` | Evidence supports hypothesis | `stance_confidence` 0.0-1.0 |
| `REFUTES` | Evidence contradicts hypothesis | `stance_confidence` 0.0-1.0 |
| `NEUTRAL` | Evidence neither supports nor refutes | — |
