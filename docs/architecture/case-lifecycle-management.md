# Case Lifecycle Management
## Case Status State Machine and Transition Rules

**Document Type:** Component Specification
**Version:** 1.0
**Last Updated:** 2025-10-11
**Status:** ✅ **IMPLEMENTED** (v3.2.0)

## Implementation Status

**Implementation Date:** 2025-10-11
**Implementation Version:** v3.2.0
**Status:** Integrated with OODA framework

**Implementation Summary:**
- ✅ Case status state machine implemented in investigation lifecycle
- ✅ Status transitions handled by phase handlers
- ✅ InvestigationLifecycle.case_status field tracks current state
- ✅ Stall detection logic ready for implementation
- ✅ Status updates integrated with phase completion

**Implementation Files:**
- Lifecycle Model: `faultmaven/models/investigation.py` (InvestigationLifecycle)
- Phase Handlers: All handlers update case_status on phase transitions
- Solution Handler: Sets status to "resolved" on fix verification

**Note:** Case status (intake, in_progress, resolved, etc.) is orthogonal to investigation phases (0-6) and tracks overall case lifecycle state.

---

## Document Scope

### Purpose

This document defines the **case status state machine** that tracks the lifecycle of troubleshooting investigations independent of investigation phases and OODA execution.

**Key Distinction**:
- **Investigation Phases (0-6)**: Process stages WITHIN investigation (Intake→Document)
- **Case Status**: Overall case state ACROSS investigation lifecycle

**Example**: A case can be in status `IN_PROGRESS` while progressing through investigation Phase 2 (Timeline) → Phase 3 (Hypothesis) → Phase 4 (Validation).

### Related Documents

- **[Investigation Phases and OODA Integration Framework](./investigation-phases-and-ooda-integration.md)** - Defines investigation phases (orthogonal to case status)
- **[Evidence Collection and Tracking Design](./evidence-collection-and-tracking-design.md)** - References case status for strategy selection

---

## Case Status Model

### CaseStatus Enum

```python
class CaseStatus(str, Enum):
    """Case lifecycle status - orthogonal to investigation phases"""
    
    INTAKE = "intake"              # Gathering initial problem information (Phase 0)
    IN_PROGRESS = "in_progress"    # Active investigation underway (Phases 1-5)
    MITIGATED = "mitigated"        # Service restored but root cause unknown (Active Incident only)
    RESOLVED = "resolved"          # Root cause identified and fix applied (Phase 5 complete)
    STALLED = "stalled"            # Investigation blocked (evidence unavailable, hypotheses exhausted)
    ABANDONED = "abandoned"        # User disengaged without resolution
    CLOSED = "closed"              # Formally closed with or without artifacts (Phase 6 complete)
```

### Status Categories

**Active States** (Investigation ongoing):
- `INTAKE` - Phase 0, problem confirmation pending
- `IN_PROGRESS` - Phases 1-5, active investigation
- `STALLED` - Investigation blocked, needs intervention

**Terminal States** (Investigation complete):
- `MITIGATED` - Service restored (can transition to post-mortem)
- `RESOLVED` - Root cause found and fixed
- `ABANDONED` - User gave up
- `CLOSED` - Final state after artifacts/closure

---

## State Transition Rules

### State Machine Diagram

```
    START
      │
      ▼
   INTAKE ────[problem confirmed]───> IN_PROGRESS
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    │                      │                      │
       [service restored]          [root cause found]       [cannot progress]
       (Active Incident)                   │                      │
                    │                      │                      │
                    ▼                      ▼                      ▼
               MITIGATED                RESOLVED               STALLED
                    │                      │                      │
                    │                      │              [new evidence/path]
                    │                      │                      │
                    │                      │                      ▼
                    │                      │                 IN_PROGRESS
                    │                      │                      │
                    │                      │               [user gives up]
                    │                      │                      │
                    └──[post-mortem]───────┴──[close case]───────┤
                             │                                    │
                             ▼                                    ▼
                          CLOSED <─────────────────────────── ABANDONED
```

### Valid Transitions

| From State | To States | Trigger |
|------------|-----------|---------|
| **INTAKE** | IN_PROGRESS | User confirms problem, advances to Phase 1 |
| **INTAKE** | ABANDONED | User decides not to investigate |
| **IN_PROGRESS** | MITIGATED | Service restored (Active Incident Strategy only) |
| **IN_PROGRESS** | RESOLVED | Root cause found, solution applied and verified (Phase 5 complete) |
| **IN_PROGRESS** | STALLED | Stall condition detected |
| **IN_PROGRESS** | ABANDONED | User disengages |
| **STALLED** | IN_PROGRESS | New evidence path found OR user provides blocked evidence |
| **STALLED** | ABANDONED | User gives up after stall |
| **STALLED** | CLOSED | Administrative closure |
| **MITIGATED** | IN_PROGRESS | Begin post-mortem investigation (Phases 2-4) |
| **MITIGATED** | CLOSED | User declines post-mortem, closes case |
| **RESOLVED** | CLOSED | Phase 6 complete (artifacts generated or declined) |
| **ABANDONED** | CLOSED | Administrative cleanup |

### Transition Actions

| Transition | Actions Required |
|------------|------------------|
| INTAKE → IN_PROGRESS | Create InvestigationState, activate Lead Investigator Mode, select investigation strategy, advance to Phase 1 |
| IN_PROGRESS → MITIGATED | Record mitigation actions, transition investigation strategy to POST_MORTEM, update urgency to NORMAL |
| IN_PROGRESS → RESOLVED | Record root cause conclusion (with confidence), prepare for Phase 6 (Document) |
| IN_PROGRESS → STALLED | Record stall reason, notify user, offer options (alternatives, escalation, close) |
| STALLED → IN_PROGRESS | Clear stall reason, resume investigation from appropriate phase |
| MITIGATED → IN_PROGRESS | Begin Phase 2-4 (post-mortem analysis), strategy already set to POST_MORTEM |
| RESOLVED/MITIGATED → CLOSED | Generate artifacts if user requested (Phase 6), archive investigation data |
| STALLED/ABANDONED → CLOSED | Record final state, archive partial investigation data |

---

## Stall Detection

### Stall Conditions

Investigation enters `STALLED` status when:

1. **Multiple Critical Evidence Blocked**:
   - ≥3 critical evidence requests have status BLOCKED
   - Categories: SYMPTOMS, CONFIGURATION considered critical
   
2. **All Hypotheses Refuted** (Phase 4 - Validation):
   - ≥3 hypotheses tested
   - All have status "refuted"
   - No viable alternative hypotheses

3. **No Progress for Extended Period**:
   - ≥5 conversation turns without phase advancement
   - Evidence loop detected (requesting same evidence repeatedly)
   - User providing same information without new insights

4. **Hypothesis Generation Failure** (Phase 3):
   - Phase 3 active for ≥3 turns
   - No hypotheses generated
   - Insufficient evidence to formulate theories

### Stall Detection Algorithm

```python
def check_for_stall(state: InvestigationState) -> Optional[str]:
    """
    Determine if investigation has stalled, return reason if so.
    
    Investigation Phases 0-5 subject to stall detection.
    Phase 6 (Document) exempt (post-resolution).
    
    See investigation-phases-and-ooda-integration.md for phase definitions.
    """
    
    # Validate phase bounds
    if not (0 <= state.current_phase <= 5):
        raise ValueError(f"Invalid phase: {state.current_phase}. Must be 0-5 for stall detection.")
    
    # Condition 1: Multiple critical evidence blocked
    evidence_layer = state.evidence_layer
    blocked_critical = [
        req for req in evidence_layer.evidence_requests
        if req.status == EvidenceStatus.BLOCKED
        and req.category in [EvidenceCategory.SYMPTOMS, EvidenceCategory.CONFIGURATION]
    ]
    if len(blocked_critical) >= 3:
        return "Multiple critical evidence sources blocked (cannot access logs, configs, metrics)"
    
    # Condition 2: All hypotheses refuted (Phase 4 only)
    if state.current_phase == 4:  # Phase 4: Validation
        ooda_layer = state.ooda_engine_layer
        if len(ooda_layer.hypotheses) >= 3:
            all_refuted = all(h.status == "refuted" for h in ooda_layer.hypotheses)
            if all_refuted:
                return "All formulated hypotheses have been refuted by evidence"
    
    # Condition 3: No progress for extended period
    lifecycle_layer = state.lifecycle_layer
    turns_in_phase = lifecycle_layer.metadata.get("turns_in_current_phase", 0)
    if turns_in_phase >= 5 and not lifecycle_layer.metadata.get("phase_progress_detected"):
        return "No investigation progress after 5 turns (possible evidence loop or dead end)"
    
    # Condition 4: Hypothesis generation failure (Phase 3 only)
    if state.current_phase == 3:  # Phase 3: Hypothesis
        if len(ooda_layer.hypotheses) == 0 and turns_in_phase >= 3:
            return "Unable to formulate hypotheses with available evidence"
    
    return None  # No stall detected
```

### Stall Response

When stall detected:

1. **Update Status**: Set `case_status = CaseStatus.STALLED`
2. **Record Reason**: Store stall reason in state
3. **Notify User**: Present stall situation clearly
4. **Offer Options**:
   - Explore alternative investigation paths
   - Attempt to obtain blocked evidence (different approach)
   - Recommend escalation to human expert
   - Close case if user chooses

---

## Status-Based Behaviors

### INTAKE Status

**Investigation State**: Phase 0 (problem confirmation)  
**Engagement Mode**: Consultant  
**User Can**: 
- Provide more problem details
- Consent to investigation
- Decide not to investigate

**System Behavior**:
- Listen for problem signals
- Build ProblemConfirmation
- Offer investigation start

---

### IN_PROGRESS Status

**Investigation State**: Phases 1-5 (active investigation)  
**Engagement Mode**: Lead Investigator  
**User Can**:
- Provide evidence
- Answer questions
- Report evidence unavailable
- Request escalation

**System Behavior**:
- Execute OODA iterations
- Generate evidence requests
- Test hypotheses
- Track progress

---

### MITIGATED Status

**Investigation State**: Service restored, optional post-mortem  
**Investigation Strategy**: Transitioned to POST_MORTEM  
**User Can**:
- Begin post-mortem investigation (return to IN_PROGRESS)
- Close case without RCA
- Defer RCA to later

**System Behavior**:
- Offer post-mortem analysis
- Can resume at Phase 2-4 for RCA
- No urgency pressure

---

### RESOLVED Status

**Investigation State**: Root cause found, fix applied  
**Current Phase**: Typically Phase 5 (Solution) complete  
**User Can**:
- Request artifacts (case report, runbook)
- Close case
- Provide feedback

**System Behavior**:
- Advance to Phase 6 (Document) if artifacts requested
- Prepare case closure
- Offer knowledge base contribution

---

### STALLED Status

**Investigation State**: Blocked, cannot progress  
**Engagement Mode**: Lead Investigator (seeking resolution)  
**User Can**:
- Provide previously blocked evidence
- Try alternative investigation path
- Request escalation
- Abandon case

**System Behavior**:
- Present stall reason clearly
- Suggest alternatives
- Recommend escalation if critical
- Support user decision

---

### ABANDONED Status

**Investigation State**: User disengaged  
**Transition Path**: → CLOSED  
**System Behavior**:
- Record final investigation state
- Preserve partial investigation data
- Enable case reopening if needed

---

### CLOSED Status (Terminal)

**Investigation State**: Final state  
**Transitions**: None (terminal)  
**Data Retention**: Archive per retention policy  
**Artifacts**: Case report, runbook (if generated)

---

## Case Status vs Investigation Phase

### Orthogonal Relationship

Case Status and Investigation Phases are **independent dimensions**:

| Investigation Phase | Possible Case Statuses |
|-------------------|----------------------|
| **Phase 0: Intake** | INTAKE, ABANDONED |
| **Phase 1: Blast Radius** | IN_PROGRESS, STALLED, ABANDONED |
| **Phase 2: Timeline** | IN_PROGRESS, STALLED, ABANDONED |
| **Phase 3: Hypothesis** | IN_PROGRESS, STALLED, ABANDONED |
| **Phase 4: Validation** | IN_PROGRESS, STALLED, ABANDONED |
| **Phase 5: Solution** | IN_PROGRESS, MITIGATED, RESOLVED, ABANDONED |
| **Phase 6: Document** | RESOLVED, MITIGATED, CLOSED |

**Example Scenarios**:
- Case at Phase 3 (Hypothesis), status IN_PROGRESS → Normal investigation
- Case at Phase 4 (Validation), status STALLED → Evidence blocked, can't test hypotheses
- Case at Phase 5 (Solution), status MITIGATED → Service restored, verifying fix
- Case at Phase 6 (Document), status RESOLVED → Generating artifacts before closure

---

## Integration Points

### With Investigation Phases Framework

**Case status changes triggered by phase transitions**:
- Phase 0 complete → INTAKE to IN_PROGRESS
- Phase 5 complete → IN_PROGRESS to RESOLVED or MITIGATED
- Phase 6 complete → RESOLVED/MITIGATED to CLOSED

**Stall detection uses investigation phase context**:
- Different stall conditions per phase
- Phase-specific timeout thresholds
- Evidence completeness requirements vary by phase

### With Evidence Collection Design

**Investigation strategy selection uses case status**:
```python
# When transitioning to Lead Investigator Mode
if urgency == CRITICAL and case_status != MITIGATED:
    strategy = InvestigationStrategy.ACTIVE_INCIDENT
else:
    strategy = InvestigationStrategy.POST_MORTEM
```

**Stall detection uses evidence state**:
- Blocked evidence count
- Evidence coverage score
- Critical evidence availability

---

## API Integration

### ViewState Representation

```json
{
  "view_state": {
    "case_id": "case-xyz789",
    "case_status": "in_progress",        // Case Status (this document)
    "lifecycle_progress": {
      "current_phase": 3,                 // Investigation Phase (0-6)
      "phase_name": "hypothesis",
      "phase_complete": false
    },
    "engagement_mode": "investigator",
    "investigation_strategy": "post_mortem"
  }
}
```

**Field Definitions**:
- `case_status`: Case lifecycle state (7 values from this document)
- `current_phase`: Investigation phase number (0-6, see Investigation Phases Framework)
- `engagement_mode`: Consultant or Lead Investigator (see Investigation Phases Framework)
- `investigation_strategy`: Active Incident or Post-Mortem (see Evidence Collection Design)

---

## Status Reporting

### User-Facing Status Messages

| Case Status | User Message | Next Actions |
|-------------|-------------|--------------|
| INTAKE | "Gathering initial information" | Confirm problem to start investigation |
| IN_PROGRESS | "Investigation in progress (Phase {N})" | Provide requested evidence, answer questions |
| MITIGATED | "Service restored, root cause analysis available" | Start post-mortem or close case |
| RESOLVED | "Root cause identified and resolved" | Generate artifacts or close case |
| STALLED | "Investigation blocked: {reason}" | Provide alternatives, escalate, or close |
| ABANDONED | "Investigation discontinued" | Reopen if needed |
| CLOSED | "Case closed" | View artifacts, reopen if issue returns |

---

## Escalation Management

### Escalation Triggers

Escalation is recommended when investigation reaches impasse or requires specialized expertise.

**5 Escalation Conditions**:

1. **Too Many Iterations Without Progress**
   - ≥10 OODA iterations without root cause identification
   - Trigger: Exhausted reasonable investigation paths
   - Action: Escalate to senior SRE or specialist

2. **Critical Evidence Blocked**
   - Cannot access required logs, configs, or metrics
   - Trigger: User reports "cannot access", "permission denied", "no access"
   - Action: Escalate to team with proper access

3. **Multiple Mitigation Failures** (Active Incident only)
   - ≥3 mitigation attempts failed in Phase 5
   - Trigger: Service still down after multiple fix attempts
   - Action: Escalate to on-call lead or incident commander

4. **User Explicit Request**
   - User explicitly asks to escalate
   - Trigger: "escalate" keyword in user input
   - Action: Immediately offer escalation guidance

5. **Investigation Stalled** (No Progress)
   - No confidence increase in last 3 OODA iterations
   - No new meaningful evidence in 5 turns
   - Trigger: Stall detection algorithm
   - Action: Offer alternatives or escalate

### Escalation Decision Algorithm

```python
def should_escalate(state: InvestigationState) -> tuple[bool, Optional[str]]:
    """
    Determine if investigation should be escalated
    
    Returns:
        (should_escalate, reason)
    """
    
    # Condition 1: Too many iterations
    if state.current_ooda_iteration >= 10:
        return True, "Unable to identify root cause after 10 OODA iterations"
    
    # Condition 2: Critical evidence blocked
    if _is_critical_evidence_blocked(state):
        return True, "Critical evidence required but inaccessible"
    
    # Condition 3: Multiple mitigation failures
    if state.current_phase == 5 and state.investigation_strategy == "active_incident":
        mitigation_attempts = _count_mitigation_attempts(state)
        if mitigation_attempts >= 3:
            return True, f"{mitigation_attempts} mitigation attempts failed"
    
    # Condition 4: User requested
    last_input = state.conversation_history[-1] if state.conversation_history else None
    if last_input and "escalate" in last_input.get("content", "").lower():
        return True, "User requested escalation"
    
    # Condition 5: Investigation stalled
    if _is_investigation_stalled(state):
        return True, "No progress in last 3 iterations"
    
    return False, None
```

### Escalation Context Generation

When escalation is triggered, provide comprehensive handoff context:

```python
def generate_escalation_context(state: InvestigationState) -> Dict:
    """Generate escalation handoff context"""
    
    return {
        # Problem summary
        "problem_summary": state.anomaly_frame.statement if state.anomaly_frame else state.problem_statement,
        "severity": state.anomaly_frame.severity if state.anomaly_frame else "unknown",
        
        # Investigation progress
        "investigation_duration": calculate_duration(state),
        "evidence_collected": len(state.evidence_items),
        "hypotheses_tested": sum(1 for h in state.hypotheses if h.tested),
        "current_phase": state.current_phase,
        "ooda_iterations": state.current_ooda_iteration,
        
        # Current status
        "mitigation_attempted": state.mitigation_applied is not None,
        "case_status": state.case_status,
        
        # Escalation routing
        "recommended_escalation_target": determine_escalation_target(state),
        "summary_for_escalation": generate_escalation_summary(state)
    }
```

### Intelligent Escalation Routing

Route escalation to appropriate team based on affected components:

```python
def determine_escalation_target(state: InvestigationState) -> str:
    """Determine who/what to escalate to based on context"""
    
    if state.anomaly_frame:
        components = state.anomaly_frame.affected_components
        
        # Database-related issues
        if any("database" in c.lower() or "db" in c.lower() for c in components):
            return "Database Team / DBA"
        
        # Network-related issues
        if any("network" in c.lower() for c in components):
            return "Network Operations / Infrastructure Team"
        
        # Kubernetes/container issues
        if any("kubernetes" in c.lower() or "k8s" in c.lower() or "docker" in c.lower() for c in components):
            return "Platform Team / SRE"
        
        # Cloud provider issues
        if any("aws" in c.lower() or "azure" in c.lower() or "gcp" in c.lower() for c in components):
            return "Cloud Infrastructure Team"
    
    # Default escalation
    return "Senior SRE / On-Call Lead"
```

### Escalation Summary Format

```
⚠️ ESCALATION RECOMMENDED

Problem: API returning 500 errors in EU region
Severity: High
Duration: 1h 23m

Investigation Progress:
• Evidence collected: 8 items
• Hypotheses tested: 4
• Current phase: 4 (Validation)
• OODA iterations: 7

What we've tried:
  ✗ Hypothesis #1: Database pool exhaustion (refuted by metrics)
  ✗ Hypothesis #2: Memory leak (inconclusive, need heap dump)
  ✗ Hypothesis #3: Network issue (refuted by connectivity tests)
  ? Hypothesis #4: Rate limiting bug (testing blocked - need production access)

Blocking Issue:
Cannot access production logs and metrics. Need elevated permissions.

Recommended Contact:
Platform Team / SRE with production access

Next Steps:
Share this conversation history for full context.
```

### Escalation State Transition

When escalation occurs, case status should reflect it:

```python
# Mark case as requiring escalation
state.escalation_recommended = True
state.escalation_reason = "Critical evidence blocked"
state.escalation_target = "Database Team"

# Case status remains IN_PROGRESS or transitions to STALLED
if critical_blocking:
    state.case_status = CaseStatus.STALLED
```

**Note**: Escalation doesn't automatically close the case. User may choose to:
- Continue investigation with new information
- Hand off to escalation target (transition to ABANDONED or CLOSED)
- Wait for access and resume (STALLED → IN_PROGRESS)

---

## Document Metadata

**Document Type**: Component Specification  
**Extracted From**: Evidence-Centric Troubleshooting Design v2.0  
**Related To**: Investigation Phases Framework, Evidence Collection Design  
**Purpose**: Centralize case status management separate from investigation phase progression

**Version History**:
- v1.0 (2025-10-10): Initial extraction with status state machine
- v1.1 (2025-10-11): Added escalation management section

---

**END OF DOCUMENT**

