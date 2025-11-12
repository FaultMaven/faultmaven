# FaultMaven Investigation State and Control Framework v2.0

## Executive Summary

This document defines the **complete investigation workflow** for FaultMaven, specifying how cases progress through phases, how decisions are made, and how the system adapts to different investigation scenarios.

**v2.0 Changes**: Integrated all updates from v1.1 into a single, complete reference document. Added 10 critical enhancements:
1. Phase 0→1 transition requires explicit user decision
2. Correlation confidence calculation for URGENT routing
3. Hypothesis generation mode usage rules
4. Evidence request lifecycle management
5. Solution type logic (URGENT vs NON_URGENT)
6. Documentation capability assessment rules
7. OODA iteration exit criteria
8. Confidence threshold mappings and calibration
9. Root cause synchronization with working conclusion
10. NEW Section 6: Degraded mode handling

**Key Principles**:
- **Phase-Based Structure**: Investigation flows through 7 phases (0-6)
- **Adaptive Routing**: URGENT vs NON_URGENT paths based on problem severity
- **OODA Loops**: Tactical progress within strategic phases
- **Bidirectional with Case Data**: Framework prescribes, Case data informs
- **Resilience**: Degraded mode enables progress when blocked

**Complete Phase Structure**:
```
Phase 0: Pre-Investigation (Problem Confirmation)
Phases 1-5: Active Investigation
  ├── Phase 1: Triage & Impact Assessment
  ├── Phase 2: Timeline Construction
  ├── Phase 3: Hypothesis Generation
  ├── Phase 4: Diagnosis (Evidence Collection)
  └── Phase 5: Solution Implementation
Phase 6: Documentation & Knowledge Capture
```

**Related Documents**:
- [Case Data Model v1.4](./case-data-model.md) - Data structures that support this framework
- [Data Preprocessing Architecture v2.0](./data-preprocessing-architecture.md)
- [Evidence Architecture v1.1](./evidence-architecture.md)

---

## Table of Contents

1. [Framework Overview](#1-framework-overview)
2. [Investigation Routing & Urgency](#2-investigation-routing--urgency)
3. [Phase Definitions](#3-phase-definitions)
4. [OODA Loop Integration](#4-ooda-loop-integration)
5. [Cross-Phase State](#5-cross-phase-state)
6. [Degraded Mode Handling](#6-degraded-mode-handling)
7. [State Management & Transitions](#7-state-management--transitions)
8. [Examples & Scenarios](#8-examples--scenarios)

---

## 1. Framework Overview

### 1.1 Purpose

The Investigation Framework provides:
1. **Structure**: Clear phases with defined objectives
2. **Flexibility**: Adaptive routing based on urgency
3. **Efficiency**: Skip unnecessary phases when appropriate
4. **Quality**: Ensure thorough investigation when needed
5. **Resilience**: Continue investigation even when blocked

### 1.2 Core Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Phase 0: Pre-Investigation (Problem Confirmation)          │
│   - Consulting conversation                                 │
│   - Problem detection                                       │
│   - Decision to investigate                                 │
└────────────────────┬────────────────────────────────────────┘
                     │
         User explicitly decides to investigate?
                     │ YES
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ Phase 1: Triage & Impact Assessment                        │
│   - Define problem (AnomalyFrame)                          │
│   - Assess severity and scope                              │
│   - Determine urgency level                                │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ Phase 2: Timeline Construction                             │
│   - Build chronological event sequence                     │
│   - Detect correlations                                    │
│   - Calculate correlation_confidence                       │
└────────────────────┬────────────────────────────────────────┘
                     │
              ┌──────┴──────┐
              │             │
    URGENT?   │             │   NON_URGENT?
    (correlation > 0.9)     │   (thorough investigation)
              │             │
              ↓             ↓
         Skip to      Phase 3: Hypothesis Generation
         Phase 5      - Generate testable theories
              │       - Define evidence requirements
              │             │
              │             ↓
              │       Phase 4: Diagnosis
              │       - Collect evidence
              │       - Test hypotheses
              │       - Validate root cause
              │             │
              └─────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ Phase 5: Solution Implementation                           │
│   - Apply immediate solution (URGENT)                      │
│   - Implement longterm fix (NON_URGENT)                   │
│   - Verify solution effectiveness                          │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ Phase 6: Documentation & Knowledge Capture                 │
│   - Generate documentation (capability-assessed)           │
│   - Capture lessons learned                                │
│   - Enrich knowledge base                                  │
└─────────────────────────────────────────────────────────────┘
```

### 1.3 Investigation Paths

| Path | Route | Phases | Use Case | Timeline |
|------|-------|--------|----------|----------|
| **Consulting-Only** | 0→6 | Phase 0, 6 | Quick Q&A, no formal investigation | Minutes |
| **URGENT** | 0→1→2→5→6 | Skip hypotheses/evidence | Production incident | Hours |
| **NON_URGENT** | 0→1→2→3→4→5→6 | Full investigation | Complex root cause | Days |
| **Post-Mortem** | 0→1→2→3→4→6 | After-the-fact analysis | Historical incident | Days |

---

## 2. Investigation Routing & Urgency

### 2.1 Urgency Determination

**Purpose**: Classify problem urgency to select appropriate investigation path.

**Urgency Levels**:
```python
class UrgencyLevel(str, Enum):
    CRITICAL = "critical"  # Total outage, immediate action required
    HIGH = "high"          # Major impact, rapid response needed
    MEDIUM = "medium"      # Moderate impact, standard investigation
    LOW = "low"            # Minor impact, can be deferred
    UNKNOWN = "unknown"    # Not yet determined
```

**Urgency Signals** (from Phase 0):

```python
class UrgencySignals(BaseModel):
    """Signals used to determine urgency"""
    
    urgency_hint: str = Field(
        description="high | medium | low | none"
    )
    temporal_hint: str = Field(
        description="active | historical"
    )
    scope_hint: str = Field(
        description="total | partial | isolated | none"
    )
```

**Determination Logic**:

```python
def determine_urgency_level(signals: UrgencySignals) -> UrgencyLevel:
    """
    Determine urgency from signals.
    
    Critical triggers:
    - urgency_hint = "high" AND temporal_hint = "active" AND scope_hint = "total"
    - Words like "down", "outage", "can't access", "total failure"
    
    High triggers:
    - urgency_hint = "high" AND temporal_hint = "active"
    - scope_hint = "partial" with high severity
    
    Medium/Low:
    - temporal_hint = "historical"
    - scope_hint = "isolated"
    - No urgency keywords
    """
    
    # CRITICAL: Total active outage
    if (signals.urgency_hint == "high" and 
        signals.temporal_hint == "active" and 
        signals.scope_hint == "total"):
        return UrgencyLevel.CRITICAL
    
    # HIGH: Active problem with significant impact
    if (signals.urgency_hint == "high" and 
        signals.temporal_hint == "active"):
        return UrgencyLevel.HIGH
    
    # MEDIUM: Historical or moderate impact
    if signals.urgency_hint == "medium":
        return UrgencyLevel.MEDIUM
    
    # LOW: Historical or isolated issue
    if (signals.temporal_hint == "historical" or 
        signals.scope_hint == "isolated"):
        return UrgencyLevel.LOW
    
    return UrgencyLevel.UNKNOWN
```

### 2.2 Investigation Strategy Selection

**Two Strategies**:

```python
class InvestigationStrategy(str, Enum):
    URGENT = "urgent"          # Speed priority: 1→2→5
    NON_URGENT = "non_urgent"  # Thoroughness priority: 1→2→3→4→5
```

**Selection Logic**:

```python
def select_investigation_strategy(
    urgency_level: UrgencyLevel,
    correlation_confidence: float
) -> InvestigationStrategy:
    """
    Select investigation strategy.
    
    URGENT if:
    - CRITICAL urgency + high correlation confidence (>0.9)
    - Obvious cause that needs immediate mitigation
    
    NON_URGENT otherwise:
    - Need to understand root cause thoroughly
    - No obvious correlation
    - Post-mortem analysis
    """
    
    # URGENT: Critical + obvious cause
    if (urgency_level == UrgencyLevel.CRITICAL and 
        correlation_confidence > 0.9):
        return InvestigationStrategy.URGENT
    
    # NON_URGENT: Everything else
    return InvestigationStrategy.NON_URGENT
```

**Strategy Implications**:

| Aspect | URGENT | NON_URGENT |
|--------|--------|------------|
| **Goal** | Stop the bleeding | Understand root cause |
| **Phases** | 1→2→5 | 1→2→3→4→5 |
| **Solution** | Immediate only | Both immediate + longterm |
| **Verification** | Optional | Required |
| **Timeline** | Minutes-Hours | Hours-Days |
| **Documentation** | Incident report | Full post-mortem |

### 2.3 Phase 0→1 Transition Criteria

**Two Conditions Required** (BOTH must be true):

**1. Problem Confirmation Exists**:
```python
case.consulting.problem_confirmation is not None
```

The problem confirmation structure contains:
- `problem_statement`: Clear description of the issue
- `severity`: high | medium | low
- `investigation_approach`: active_incident | post_mortem | consulting_only
- `urgency_signals`: Structured urgency indicators

**2. User Decision to Investigate**:
```python
case.consulting.decided_to_investigate == True
```

User explicitly confirmed they want formal investigation (not just exploring).

**Rationale**: 

Users may explore a problem without committing to full investigation. Without explicit decision, system would auto-transition into investigation mode, wasting resources.

**Example Conversation**:
```
User: "Sometimes my app is slow"
Agent: [Phase 0 - exploring]
  "Can you tell me more? How often? Which service?"
  
User: "About 5% of requests to auth service timeout"
Agent: [Creates problem_confirmation]
  "Would you like me to help investigate this formally?"

User: "Yes, let's figure out what's causing it"
  [Sets decided_to_investigate = True]
  
Agent: [Transition to Phase 1]
  "Great, let's start by confirming the scope..."
```

**Validation Function**:
```python
def can_transition_to_phase_1(case: Case) -> bool:
    """
    Phase 0 → Phase 1 requires both:
    1. Problem confirmation exists
    2. User explicitly decided to investigate
    """
    return (
        case.consulting.problem_confirmation is not None and
        case.consulting.decided_to_investigate
    )
```

---

## 3. Phase Definitions

### 3.0 Phase 0: Pre-Investigation (Problem Confirmation)

**Objective**: Detect problems, provide consulting, determine if formal investigation needed.

**Entry**: User starts conversation

**Key Activities**:
1. Listen to user's problem description
2. Ask clarifying questions
3. Provide quick guidance/consulting
4. Determine if formal investigation needed
5. If yes, structure problem confirmation
6. Get explicit user decision to investigate

**Data Collected**:
```python
class ConsultingData:
    consultation_summary: str           # AI summary of conversation
    problem_confirmation: ProblemConfirmation  # Structured problem
    decided_to_investigate: bool        # User decision
    key_questions_asked: List[str]      # Questions explored
    resources_suggested: List[str]      # KB articles suggested
```

**Exit Criteria**:
- **To Phase 1**: `problem_confirmation` exists AND `decided_to_investigate = True`
- **To Phase 6**: User decides no investigation needed (consulting-only)

**Example Flow**:
```
User: "My dashboard is showing weird numbers"

Agent: "Can you describe what looks wrong? Are other users seeing this?"

User: "The user count dropped to zero suddenly. Other metrics look normal."

Agent: "That sounds concerning. Let me check a few things..."
      [Asks diagnostic questions]
      [Creates problem_confirmation]
      "It seems your user count metric is returning zero. 
       Would you like to investigate this formally?"

User: "Yes please"
      [decided_to_investigate = True]

Agent: [Transition to Phase 1]
```

---

### 3.1 Phase 1: Triage & Impact Assessment

**Objective**: Understand what's broken and assess impact.

**Entry**: Phase 0 with investigation decision

**Key Activities**:
1. Define problem statement clearly
2. Identify affected components
3. Assess user and business impact
4. Classify severity and scope
5. Verify problem with data
6. Determine urgency level

**Data Collected**:
```python
class AnomalyFrame:
    problem_statement: str                    # What's observably wrong
    confidence: float                         # Confidence in description
    severity: str                            # critical|high|medium|low
    scope: str                               # single_service|multiple_services|system_wide
    affected_components: List[str]           # Services/systems affected
    user_impact: Optional[str]               # User impact description
    business_impact: Optional[str]           # Business impact
    symptoms: List[str]                      # Observable symptoms
    verified: bool                           # Confirmed with data
    verification_data_refs: List[str]        # References to verification data
```

**OODA Loop Activities**:
- **OBSERVE**: Gather initial symptoms, user reports
- **ORIENT**: Analyze scope and impact
- **DECIDE**: Determine severity classification
- **ACT**: Document anomaly frame, verify with data

**Exit Criteria** (v2.0 - UPDATED):
- `anomaly_frame` exists
- `anomaly_frame.verified = True`
- `anomaly_frame.confidence >= 0.6` in problem description
- `urgency_level` determined (not UNKNOWN)

**Note**: `investigation_strategy` is **NOT** determined yet (requires correlation data from Phase 2).

**Example Flow**:
```
[Phase 1 Start]

Agent: "Let's confirm the scope. You said user count shows zero. 
       Is this affecting all users or specific regions?"

User: "All users globally"

Agent: [OBSERVE] "When did this start?"

User: "About 30 minutes ago"

Agent: [ORIENT] "This appears to be a total outage of user count metrics.
       Can you upload a screenshot or metrics data?"

User: [Uploads Grafana screenshot]

Agent: [DECIDE] "Confirmed: User count metric returning 0 since 14:23 UTC.
       - Severity: HIGH
       - Scope: System-wide
       - Impact: Dashboard unusable for customer support team"
       
       [ACT] [Creates verified AnomalyFrame]

[Transition to Phase 2]
```

---

### 3.2 Phase 2: Timeline Construction

**Objective**: Build chronological event sequence and detect correlations.

**Entry**: Phase 1 with verified anomaly frame

**Key Activities**:
1. Identify when problem started vs noticed
2. Extract events from uploaded data
3. Order events chronologically
4. Detect correlations between events
5. Calculate correlation confidence
6. Determine if URGENT routing appropriate

**Data Collected**:
```python
class Timeline:
    started_at: datetime                     # When problem started
    noticed_at: datetime                     # When noticed
    duration: timedelta                      # Time gap
    events: List[TimelineEvent]              # Chronological events
    correlations: List[EventCorrelation]     # Detected correlations
    correlation_confidence: float            # Confidence in correlations (0-1)
```

**Correlation Confidence Calculation**:

This is **critical** for URGENT routing decisions.

```python
def calculate_correlation_confidence(timeline: Timeline) -> float:
    """
    Calculate confidence in event correlation.
    High confidence (>0.9) indicates obvious causal relationship.
    
    Returns:
        float: Correlation confidence score (0.0-1.0)
    """
    
    if len(timeline.correlations) == 0:
        return 0.0
    
    # Weight by correlation type
    type_weights = {
        CorrelationType.CAUSAL: 1.0,     # Strongest (proven causation)
        CorrelationType.TEMPORAL: 0.7,   # Moderate (time proximity)
        CorrelationType.SPATIAL: 0.5,    # Weakest (same system)
    }
    
    max_confidence = 0.0
    
    for corr in timeline.correlations:
        base_confidence = type_weights[corr.correlation_type]
        
        # Adjust by temporal proximity for CAUSAL/TEMPORAL
        if corr.correlation_type in [CorrelationType.CAUSAL, CorrelationType.TEMPORAL]:
            events = [e for e in timeline.events if e.event_id in corr.event_ids]
            
            if len(events) >= 2:
                time_gap = abs((events[1].timestamp - events[0].timestamp).total_seconds())
                
                # Proximity factor: closer = higher confidence
                # 5-minute (300s) decay window
                proximity_factor = 1.0 / (1 + time_gap / 300)
                adjusted_confidence = base_confidence * proximity_factor
            else:
                adjusted_confidence = base_confidence
        else:
            adjusted_confidence = base_confidence
        
        max_confidence = max(max_confidence, adjusted_confidence)
    
    return max_confidence
```

**Correlation Examples**:

**High Correlation (>0.9)** - Deployment immediately preceded errors:
```
Timeline:
  Event 1: Deployment v2.1.3 at 14:23:00
  Event 2: Error spike at 14:25:30 (150s gap)
  
Calculation:
  Type: CAUSAL (base = 1.0)
  time_gap = 150s
  proximity_factor = 1.0 / (1 + 150/300) = 0.67
  confidence = 1.0 * 0.67 = 0.67
  
Manual boost to 0.95 due to deployment timing
Result: correlation_confidence = 0.95 → URGENT path
```

**Low Correlation (<0.6)** - Config change hours before errors:
```
Timeline:
  Event 1: Config change at 08:00:00
  Event 2: Errors at 14:00:00 (21,600s gap)
  
Calculation:
  Type: TEMPORAL (base = 0.7)
  time_gap = 21,600s
  proximity_factor = 1.0 / (1 + 21600/300) = 0.014
  confidence = 0.7 * 0.014 = 0.01
  
Result: correlation_confidence = 0.01 → Continue full investigation
```

**URGENT Routing Decision**:
```python
if (case.urgency_level == UrgencyLevel.CRITICAL and 
    case.timeline.correlation_confidence > 0.9):
    # Skip to Phase 5 (URGENT path)
    case.investigation_strategy = InvestigationStrategy.URGENT
    recommend_phase_5_transition()
```

**Opportunistic Hypothesis Generation**:

If correlation confidence is high (>0.8) but not quite URGENT threshold:
```python
if timeline.correlation_confidence > 0.8:
    # Generate hypothesis opportunistically
    hypothesis = Hypothesis(
        statement="Deployment v2.1.3 introduced bug causing errors",
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        status=HypothesisStatus.CAPTURED,  # Not active yet
        likelihood=timeline.correlation_confidence,
    )
    
    present_to_user_for_confirmation()
```

**Exit Criteria** (v2.0 - UPDATED):
- `timeline` exists
- At least 2 events in timeline
- `correlation_confidence` calculated
- `investigation_strategy` determined (CRITICAL step!)

**Investigation Strategy Determination** (v2.0 - NEW):

**Critical**: Strategy is determined **at Phase 2 completion**, not during Phase 1.

```python
def complete_phase_2(case: Case):
    """
    Complete Phase 2 by calculating correlation and determining strategy.
    This must happen BEFORE routing to next phase.
    
    v2.0: Sets investigation_strategy (required for routing decision)
    """
    
    # Step 1: Calculate correlation confidence
    case.timeline.correlation_confidence = calculate_correlation_confidence(
        case.timeline
    )
    
    # Step 2: Determine investigation strategy
    case.investigation_strategy = select_investigation_strategy(
        urgency_level=case.urgency_level,
        correlation_confidence=case.timeline.correlation_confidence
    )
    
    # Validation: Must be set
    assert case.investigation_strategy is not None, "Strategy determination failed"
    
    # Step 3: Confirm with user if CRITICAL urgency + URGENT strategy
    if (case.urgency_level == UrgencyLevel.CRITICAL and
        case.investigation_strategy == InvestigationStrategy.URGENT):
        # Ask user to confirm URGENT path (1→2→5)
        # Sets routing_confirmed_by_user = True when confirmed
        present_urgent_routing_confirmation(case)
```

**Routing Decision** (executed AFTER strategy determined):
- If `investigation_strategy = URGENT` AND `urgency = CRITICAL` AND `correlation > 0.9`:
  - **Route to Phase 5** (skip Phases 3, 4)
- Otherwise:
  - **Route to Phase 3** (full investigation)

---

### 3.3 Phase 3: Hypothesis Generation

**Objective**: Generate testable theories about root cause.

**Entry**: Phase 2 complete (NON_URGENT path only)

**Entry Guard** (v2.0 - NEW):
```python
if case.investigation_strategy == InvestigationStrategy.URGENT:
    raise ValueError("URGENT path cannot enter Phase 3 - should skip to Phase 5")
```

**Rationale**: Phase 3 is for systematic hypothesis testing when cause is unclear. URGENT path already has obvious correlation from Phase 2 and skips directly to mitigation.

**Key Activities**:
1. Analyze timeline for patterns
2. Generate hypotheses systematically
3. Define evidence requirements for each
4. Prioritize hypotheses by likelihood
5. Prepare for evidence collection

**Data Collected**:
```python
class Hypothesis:
    hypothesis_id: str
    statement: str                           # Testable hypothesis
    category: str                            # code|config|environment|...
    status: HypothesisStatus                 # CAPTURED|ACTIVE|VALIDATED|...
    likelihood: float                        # Current likelihood (0-1)
    initial_likelihood: float                # Starting likelihood
    likelihood_trajectory: List[Tuple[int, float]]  # History
    generation_mode: HypothesisGenerationMode  # OPPORTUNISTIC|SYSTEMATIC|...
    evidence_requirements: List[EvidenceRequirement]  # What's needed
    evidence_completeness: float             # % requirements fulfilled
    supporting_evidence: List[str]           # Evidence IDs supporting
    refuting_evidence: List[str]             # Evidence IDs refuting
    evidence_ratio: float                    # supporting / total
    iterations_without_progress: int         # For anchoring detection
    rationale: str                          # Why generated
```

**Hypothesis Categories** (for anchoring detection):
- `code`: Software bugs, logic errors
- `config`: Configuration issues
- `environment`: OS, runtime, dependencies
- `network`: Connectivity, latency, DNS
- `data`: Database, data corruption
- `hardware`: Physical infrastructure
- `external`: Third-party services
- `human`: User error, operational mistakes
- `other`: Doesn't fit above categories

#### 3.3.1 Hypothesis Generation Modes

**Three generation modes with distinct triggers and behaviors**:

##### OPPORTUNISTIC Mode

**When**: Strong correlation detected during Phase 2  
**Trigger**: `correlation_confidence > 0.8`  
**Initial Status**: `CAPTURED` (not yet active)  
**Purpose**: Generate hypothesis when timeline shows obvious correlation

**Flow**:
```
Phase 2: Timeline shows deployment immediately before errors
  correlation_confidence = 0.95
  
Agent generates hypothesis (OPPORTUNISTIC):
  Statement: "Deployment v2.1.3 introduced bug causing errors"
  Status: CAPTURED
  Generation mode: OPPORTUNISTIC
  
Agent: "The timeline shows deployment immediately preceded errors.
       Would you like to investigate this hypothesis?"
       
User: "Yes"
  [hypothesis.status = ACTIVE]
  [May skip directly to Phase 4]
  
User: "No, let's be thorough"
  [Continue to Phase 3 for systematic generation]
```

##### SYSTEMATIC Mode

**When**: User explicitly transitions to Phase 3  
**Trigger**: Phase 2 complete, no obvious correlation  
**Initial Status**: `ACTIVE` (immediately testable)  
**Purpose**: Generate comprehensive set when cause unclear

**Flow**:
```
Phase 3: Systematic hypothesis generation
  Timeline unclear (correlation_confidence = 0.4)
  
Agent generates multiple hypotheses (SYSTEMATIC):
  
  Hypothesis 1:
    "Database connection pool exhausted"
    Status: ACTIVE
    Generation mode: SYSTEMATIC
    
  Hypothesis 2:
    "Memory leak in user service"
    Status: ACTIVE
    Generation mode: SYSTEMATIC
    
  Hypothesis 3:
    "External API dependency failure"
    Status: ACTIVE
    Generation mode: SYSTEMATIC
    
All immediately testable in Phase 4
```

##### FORCED_ALTERNATIVE Mode

**When**: User requests alternative explanations  
**Trigger**: User asks "what else could it be?" in Phase 3 or 4  
**Initial Status**: `CAPTURED` (not yet active)  
**Purpose**: Explore alternatives when primary hypothesis unlikely

**Flow**:
```
Phase 4: Testing hypothesis "Null pointer in auth code"
  Evidence shows no null pointers
  
User: "This doesn't seem right. What else could it be?"

Agent generates alternative (FORCED_ALTERNATIVE):
  Hypothesis: "Race condition in connection pool"
  Status: CAPTURED
  Generation mode: FORCED_ALTERNATIVE
  
Agent: "Another possibility is a race condition.
       Would you like to test this hypothesis?"
       
User: "Yes"
  [hypothesis.status = ACTIVE]
  [Collect evidence]
```

**Mode Summary**:

| Mode | Trigger | Initial Status | User Control | Typical Phase |
|------|---------|---------------|--------------|---------------|
| OPPORTUNISTIC | correlation > 0.8 | CAPTURED | Yes (activate?) | Phase 2 |
| SYSTEMATIC | Enter Phase 3 | ACTIVE | No (auto-active) | Phase 3 |
| FORCED_ALTERNATIVE | User request | CAPTURED | Yes (activate?) | Phase 3/4 |

#### 3.3.2 Hypothesis Lifecycle

```
CAPTURED ──(user activates)──> ACTIVE ──(evidence collected)──> VALIDATED/REFUTED
    │                             │                                     │
    │                             └─(no progress)──> RETIRED           │
    │                                                                   │
    └─(better hypothesis found)──> SUPERSEDED                         │
                                                                        │
                                                    (becomes root cause)
```

**Status Definitions**:
- `CAPTURED`: Generated but not yet being tested
- `ACTIVE`: Currently being tested (evidence collection)
- `VALIDATED`: Strong evidence supports (becomes root cause candidate)
- `REFUTED`: Strong evidence contradicts (ruled out)
- `INCONCLUSIVE`: Evidence ambiguous, neither validates nor refutes (triggers degraded mode)
- `RETIRED`: No longer relevant (insufficient evidence, moved on)
- `SUPERSEDED`: Replaced by better/more specific hypothesis

#### 3.3.3 Evidence Requirements

Each hypothesis defines what evidence would prove/disprove it:

```python
class EvidenceRequirement:
    requirement_id: str
    description: str                         # What evidence needed
    priority: RequirementPriority            # CRITICAL|HIGH|MEDIUM|LOW
    fulfilled: bool                          # Has it been provided?
    fulfilled_by: Optional[str]              # Evidence ID that fulfilled it
    acquisition_guidance: AcquisitionGuidance  # How to get it
    
class AcquisitionGuidance:
    method: str                              # "kubectl logs", "database query"
    commands: List[str]                      # Specific commands
    expected_format: str                     # "JSON logs", "CSV metrics"
    locations: List[str]                     # Where to find data
    safety_warnings: List[str]               # "Run on non-prod first"
```

**Example**:
```python
hypothesis = Hypothesis(
    statement="Null pointer in UserService.authenticate()",
    evidence_requirements=[
        EvidenceRequirement(
            requirement_id="req_001",
            description="Stack traces showing null pointer exceptions",
            priority=RequirementPriority.CRITICAL,
            acquisition_guidance=AcquisitionGuidance(
                method="grep",
                commands=["grep 'NullPointerException' /var/log/app.log"],
                expected_format="Text log lines with stack traces",
                locations=["/var/log/app.log", "Kibana logs"],
                safety_warnings=[]
            )
        ),
        EvidenceRequirement(
            requirement_id="req_002",
            description="Code diff between v2.1.2 and v2.1.3",
            priority=RequirementPriority.HIGH,
            acquisition_guidance=AcquisitionGuidance(
                method="git",
                commands=["git diff v2.1.2 v2.1.3 UserService.java"],
                expected_format="Git diff output",
                locations=["GitHub repository"],
                safety_warnings=[]
            )
        )
    ]
)
```

**Exit Criteria**:
- At least 1 hypothesis generated
- All ACTIVE hypotheses have evidence requirements defined
- Ready to collect evidence

**Next Phase**: Phase 4 (Diagnosis)

---

### 3.4 Phase 4: Diagnosis (Evidence Collection)

**Objective**: Collect data to test hypotheses and validate root cause.

**Entry**: Phase 3 with hypotheses defined (NON_URGENT path only)

**Entry Guard** (v2.0 - NEW):
```python
if case.investigation_strategy == InvestigationStrategy.URGENT:
    raise ValueError("URGENT path cannot enter Phase 4 - should skip to Phase 5")
```

**Rationale**: Phase 4 is for evidence-based hypothesis testing. URGENT path skips hypothesis generation entirely and jumps to mitigation based on timeline correlation.

**Key Activities**:
1. Convert evidence requirements into requests
2. Request evidence from user
3. Process uploaded evidence
4. Link evidence to hypotheses
5. Update hypothesis likelihood based on evidence
6. Determine if root cause validated

**Data Collected**:
```python
class Evidence:
    evidence_id: str
    request_id: Optional[str]                # Links to EvidenceRequest
    category: EvidenceCategory               # SYMPTOMS|TIMELINE|CHANGES|...
    fulfills_requirement_ids: List[str]      # Requirements fulfilled
    supports_hypothesis_ids: List[str]       # Hypotheses supported
    refutes_hypothesis_ids: List[str]        # Hypotheses refuted
    stance: Dict[str, EvidenceStance]        # Stance per hypothesis
    summary: str                             # Evidence summary
    content_ref: str                         # S3 reference
    source_type: EvidenceSourceType          # LOG_FILE|METRICS_DATA|...
    analysis: str                            # AI analysis of evidence

class EvidenceCategory(str, Enum):
    SYMPTOMS = "symptoms"                    # Problem manifestations
    TIMELINE = "timeline"                    # Temporal event data
    CHANGES = "changes"                      # Deployments, configs
    CONFIGURATION = "configuration"          # Config data
    SCOPE = "scope"                          # Impact scope data
    METRICS = "metrics"                      # Performance data
    ENVIRONMENT = "environment"              # System environment
```

#### 4.1 Evidence Request Lifecycle

**Phase 3→4 Transition: Create Requests**

When transitioning from Phase 3 to 4, convert hypothesis evidence requirements into active requests:

```python
def create_evidence_requests_from_hypotheses(case: Case):
    """
    Convert hypothesis evidence_requirements into active requests.
    Called automatically during Phase 3→4 transition.
    """
    
    for hypothesis in case.hypotheses.values():
        if hypothesis.status != HypothesisStatus.ACTIVE:
            continue
        
        for requirement in hypothesis.evidence_requirements:
            if not requirement.fulfilled:
                request = EvidenceRequest(
                    request_id=generate_id(),
                    hypothesis_id=hypothesis.hypothesis_id,
                    requirement_id=requirement.requirement_id,
                    request_text=f"Please provide: {requirement.description}",
                    requested_at_turn=case.current_turn,
                    status=EvidenceStatus.PENDING,
                    fulfilled=False,
                    acquisition_guidance=requirement.acquisition_guidance,
                )
                case.evidence_requests.append(request)
```

**Example**:
```
Phase 3: Hypothesis "Null pointer in auth code"
  Evidence Requirements:
    - req_001: "Stack traces showing null pointers"
    - req_002: "Code diff v2.1.2 → v2.1.3"
    
Phase 3→4 Transition:
  Creates Evidence Requests:
    - er_001: "Please provide: Stack traces showing null pointers"
      Status: PENDING
      Acquisition guidance: "grep 'NullPointerException' /var/log/app.log"
      
    - er_002: "Please provide: Code diff v2.1.2 → v2.1.3"
      Status: PENDING
      Acquisition guidance: "git diff v2.1.2 v2.1.3"
```

**Phase 4: Fulfill Requests**

When user uploads evidence, mark matching requests as fulfilled:

```python
def fulfill_evidence_request(case: Case, evidence: Evidence):
    """
    Mark requests as fulfilled when evidence uploaded.
    Updates both evidence_requests and hypothesis evidence tracking.
    """
    
    # Update request status
    if evidence.request_id:
        for request in case.evidence_requests:
            if request.request_id == evidence.request_id:
                request.fulfilled = True
                request.status = EvidenceStatus.COMPLETE
                request.fulfilled_by_evidence_id = evidence.evidence_id
    
    # Update requirements via fulfills_requirement_ids
    for req_id in evidence.fulfills_requirement_ids:
        for hypothesis in case.hypotheses.values():
            for req in hypothesis.evidence_requirements:
                if req.requirement_id == req_id:
                    req.fulfilled = True
                    req.fulfilled_by = evidence.evidence_id
    
    # Update hypothesis evidence tracking
    for hyp_id in evidence.supports_hypothesis_ids:
        if hyp_id in case.hypotheses:
            case.hypotheses[hyp_id].supporting_evidence.append(evidence.evidence_id)
            case.hypotheses[hyp_id].last_evidence_added_turn = case.current_turn
    
    for hyp_id in evidence.refutes_hypothesis_ids:
        if hyp_id in case.hypotheses:
            case.hypotheses[hyp_id].refuting_evidence.append(evidence.evidence_id)
            case.hypotheses[hyp_id].last_evidence_added_turn = case.current_turn
    
    # Calculate evidence ratio for affected hypotheses
    for hyp_id in set(evidence.supports_hypothesis_ids + evidence.refutes_hypothesis_ids):
        if hyp_id in case.hypotheses:
            hyp = case.hypotheses[hyp_id]
            total = len(hyp.supporting_evidence) + len(hyp.refuting_evidence)
            if total > 0:
                hyp.evidence_ratio = len(hyp.supporting_evidence) / total
            
            # Update evidence completeness
            fulfilled = sum(1 for req in hyp.evidence_requirements if req.fulfilled)
            total_reqs = len(hyp.evidence_requirements)
            hyp.evidence_completeness = fulfilled / total_reqs if total_reqs > 0 else 0.0
    
    # Track at case level
    case.evidence_provided.append(evidence.evidence_id)
    case.last_evidence_added_turn = case.current_turn
    
    # Update progress metrics
    update_evidence_progress(case)
```

**Evidence Request Status**:
```python
class EvidenceStatus(str, Enum):
    PENDING = "pending"        # Request made, awaiting evidence
    PARTIAL = "partial"        # Some evidence provided, need more
    COMPLETE = "complete"      # Requirement fully satisfied
    BLOCKED = "blocked"        # Cannot obtain (access issue)
    OBSOLETE = "obsolete"      # No longer needed (hypothesis retired)
```

**Complete Lifecycle Example**:
```
Phase 3: Hypothesis with requirements
  req_001: "Stack traces"
  req_002: "Code diff"
  
Phase 3→4 Transition: Create requests
  er_001: "Please provide: Stack traces"
  er_002: "Please provide: Code diff"
  
Phase 4: User uploads stack traces
  Evidence ev_001 uploaded
  fulfills_requirement_ids: [req_001]
  
  System updates:
  - er_001.status = COMPLETE
  - req_001.fulfilled = True
  - hypothesis.supporting_evidence.append(ev_001)
  - hypothesis.evidence_completeness = 1/2 = 0.5
  - Progress metrics: 50% complete

Phase 4: User uploads code diff
  Evidence ev_002 uploaded
  fulfills_requirement_ids: [req_002]
  
  System updates:
  - er_002.status = COMPLETE
  - req_002.fulfilled = True
  - hypothesis.evidence_completeness = 2/2 = 1.0
  - Progress metrics: 100% complete
  
Ready to validate hypothesis
```

#### 4.2 Hypothesis Validation

When sufficient evidence collected, determine if hypothesis validated:

```python
def evaluate_hypothesis_validation(hypothesis: Hypothesis) -> HypothesisStatus:
    """
    Determine if hypothesis should be VALIDATED or REFUTED.
    
    VALIDATED if:
    - evidence_ratio > 0.7 (70%+ evidence supports)
    - evidence_completeness > 0.6 (60%+ requirements fulfilled)
    - likelihood > 0.8
    
    REFUTED if:
    - evidence_ratio < 0.3 (70%+ evidence refutes)
    - Strong contradictory evidence
    """
    
    # Check for validation
    if (hypothesis.evidence_ratio > 0.7 and 
        hypothesis.evidence_completeness > 0.6 and
        hypothesis.likelihood > 0.8):
        return HypothesisStatus.VALIDATED
    
    # Check for refutation
    if hypothesis.evidence_ratio < 0.3:
        return HypothesisStatus.REFUTED
    
    # Still testing
    return HypothesisStatus.ACTIVE
```

**Root Cause Determination**:

When hypothesis validated with high confidence, create root cause conclusion:

```python
def create_root_cause_conclusion(
    case: Case,
    validated_hypothesis: Hypothesis,
    supporting_evidence: List[Evidence]
) -> RootCauseConclusion:
    """
    Create root cause conclusion from validated hypothesis.
    Synchronizes with working_conclusion.
    """
    
    # Extract evidence basis description
    evidence_basis = "\n".join([
        f"- {ev.summary}" for ev in supporting_evidence
    ])
    
    # Create root cause conclusion
    case.root_cause_conclusion = RootCauseConclusion(
        validated_hypothesis_id=validated_hypothesis.hypothesis_id,
        root_cause_statement=validated_hypothesis.statement,
        confidence=validated_hypothesis.likelihood,
        confidence_level=ConfidenceLevel.VERIFIED,
        evidence_basis=evidence_basis,
        supporting_evidence_ids=[ev.evidence_id for ev in supporting_evidence],
        contributing_factors=[],
        determined_at=datetime.now(timezone.utc),
        determined_at_turn=case.current_turn,
        determined_by=case.user_id,
    )
    
    # Synchronize working conclusion
    synchronize_working_conclusion_with_root_cause(case)
    
    return case.root_cause_conclusion
```

**Exit Criteria**:
- At least one hypothesis VALIDATED OR
- root_cause_conclusion exists

**Next Phase**:
- **Phase 5** (normal path): Implement solution based on validated hypothesis
- **Phase 6** (post-mortem path): Root cause documented, solution not needed
  - Criteria: `root_cause_conclusion` exists AND user chooses to skip solution implementation
  - Use case: Historical analysis where issue already resolved externally (e.g., by operations team)
  - User decision: "We found the root cause, but the fix is already deployed. Just document findings."

**Escalation Path**:
- All hypotheses REFUTED/RETIRED after 3 loop-backs → Escalate or close unresolved

---

### 3.5 Phase 5: Solution Implementation

**Objective**: Implement fix and verify it works.

**Entry**: Phase 4 with validated hypothesis OR Phase 2 with URGENT routing

**Dual Entry Points** (v2.0 - CLARIFIED):
- **Entry A** (NON_URGENT): From Phase 4 with validated hypothesis
- **Entry B** (URGENT): From Phase 2 with high timeline correlation

**Entry Validation**:
```python
def can_enter_phase_5(case: Case) -> bool:
    """
    Validate Phase 5 entry from either Phase 2 or Phase 4.
    """
    
    # URGENT path (from Phase 2)
    if case.investigation_strategy == InvestigationStrategy.URGENT:
        if case.current_phase != 2:
            raise ValueError(f"URGENT path enters Phase 5 from Phase 2, not {case.current_phase}")
        return (
            case.timeline is not None and
            case.timeline.correlation_confidence > 0.9
        )
    
    # NON_URGENT path (from Phase 4)
    else:
        if case.current_phase != 4:
            raise ValueError(f"NON_URGENT path enters Phase 5 from Phase 4, not {case.current_phase}")
        return (
            any(h.status == HypothesisStatus.VALIDATED 
                for h in case.hypotheses.values()) or
            case.root_cause_conclusion is not None
        )
```

**Key Activities**:
1. Propose solution(s) based on findings
2. Guide implementation
3. Apply immediate solution (URGENT) or longterm fix (NON_URGENT)
4. Verify solution effectiveness
5. Prepare for documentation

**Data Collected**:
```python
class Solution:
    immediate_solutions: List[SolutionProposal]  # Quick fixes
    longterm_solutions: List[SolutionProposal]   # Root cause fixes
    implemented_solution_ids: List[str]          # Applied solutions
    verification_data: List[VerificationData]    # Proof solution worked
    rollback_plan: Optional[str]                 # Rollback if needed
```

#### 5.1 Solution Types & Strategy

**Two Solution Types**:

```python
class SolutionType(str, Enum):
    IMMEDIATE = "immediate"    # Quick mitigation (rollback, disable, scale)
    LONGTERM = "longterm"      # Root cause fix (code fix, architecture change)
```

**Strategy-Dependent Logic**:

##### URGENT Strategy (1→2→5)

**Focus**: Stop the bleeding  
**Timeline**: Minutes-Hours  
**Solution Priority**: Immediate solutions only

**Immediate Solution Examples**:
- Rollback deployment
- Disable feature flag
- Scale resources (add servers, increase memory)
- Apply temporary patch
- Circuit breaker activation
- Failover to backup system

**Exit Criteria**:
```python
def can_exit_phase_5_urgent(case: Case) -> bool:
    """URGENT: Just need to apply solution."""
    return case.solution_applied
```

**Verification**: Optional (can verify later)

**Example Flow**:
```
Phase 2: Timeline correlation_confidence = 0.95
  Deployment at 14:23 → Errors at 14:25
  
Phase 5 (URGENT):
  Agent: "The deployment v2.1.3 appears to have caused the errors.
         Immediate solution: Rollback to v2.1.2"
         
  User: "Rolling back now... Done."
  [solution_applied = True]
  
  Agent: "Monitoring metrics... Error rate dropping to normal.
         We can document this incident now."
         
  [Transition to Phase 6 without full verification]
```

##### NON_URGENT Strategy (1→2→3→4→5)

**Focus**: Permanent resolution  
**Timeline**: Hours-Days  
**Solution Priority**: Both immediate (mitigation) + longterm (fix)

**Typical Sequence**:
1. **Immediate solution**: Stop current impact
2. **Longterm solution**: Fix root cause
3. **Verification**: Confirm both work

**Exit Criteria**:
```python
def can_exit_phase_5_non_urgent(case: Case) -> bool:
    """NON_URGENT: Need application AND verification."""
    return case.solution_applied and case.solution_verified
```

**Verification**: Required before closure

**Example Flow**:
```
Phase 4: Root cause validated
  "Database connection pool size too small"
  
Phase 5 (NON_URGENT):
  Step 1: Immediate mitigation
    Agent: "Immediate solution: Restart service to clear stuck connections"
    User: "Restarted. Service stable now."
    [solution_applied = True]
    
  Step 2: Longterm fix
    Agent: "Longterm solution: Increase pool size from 10 to 50"
    User: "Updated config, deployed v2.1.4"
    
  Step 3: Verification
    Agent: "Let's verify with 24 hours of metrics..."
    User: [Uploads 24h metrics showing stable connections]
    [solution_verified = True]
    
  [Transition to Phase 6 with full verification]
```

**Unified Transition Logic**:

```python
def can_transition_to_phase_6(case: Case) -> bool:
    """
    Unified Phase 5→6 transition logic.
    Adapts criteria based on investigation strategy.
    """
    
    if case.investigation_strategy == InvestigationStrategy.URGENT:
        # URGENT: Fast exit (solution_applied only)
        return case.solution_applied
    else:
        # NON_URGENT: Complete exit (solution_applied + verified)
        return case.solution_applied and case.solution_verified
```

**Strategy Comparison**:

| Aspect | URGENT | NON_URGENT |
|--------|--------|------------|
| **Solution Focus** | Immediate only | Both immediate + longterm |
| **Exit Criteria** | solution_applied | solution_applied + verified |
| **Verification** | Optional (later) | Required (before close) |
| **Documentation** | Quick incident report | Full post-mortem |
| **Timeline** | Minutes-Hours | Hours-Days |
| **Example** | Rollback bad deploy | Fix connection pool bug |

**Exit Criteria**:
- URGENT: `solution_applied = True`
- NON_URGENT: `solution_applied = True` AND `solution_verified = True`

**Next Phase**: Phase 6 (Documentation)

---

### 3.6 Phase 6: Documentation & Knowledge Capture

**Objective**: Document lessons learned and enrich organizational knowledge.

**Entry**: Phase 5 with solution applied (and verified if NON_URGENT)

**Key Activities**:
1. Assess documentation capabilities based on path taken
2. Generate appropriate documentation
3. Capture lessons learned
4. Enrich knowledge base with findings
5. Close case

**Data Collected**:
```python
class DocumentationData:
    entry_path_to_phase_6: Phase6EntryPath      # How we got here
    documentation_state: DocumentationState      # What can be generated
    knowledge_base_enrichment: KBEnrichment      # KB updates
    lessons_learned: str                         # Key takeaways
    frozen_working_conclusion: str               # Snapshot
    frozen_progress_metrics: Dict               # Snapshot
```

#### 6.1 Documentation Capability Assessment

**Critical**: Assess what documents CAN be generated based on collected data.

```python
def assess_documentation_capabilities(case: Case) -> DocumentationState:
    """
    Determine what documents can be generated.
    Prevents incomplete or misleading documentation.
    """
    
    state = DocumentationState()
    
    # Incident Report (requires Phase 1 data)
    state.incident_report_available = (case.anomaly_frame is not None)
    if not state.incident_report_available:
        state.unavailable_reasons["incident_report"] = (
            "No triage data collected (Phase 1 skipped)"
        )
    
    # Runbook (requires Phase 5 data)
    state.runbook_available = (
        case.solution is not None and
        len(case.solution.immediate_solutions) + 
        len(case.solution.longterm_solutions) > 0
    )
    if not state.runbook_available:
        state.unavailable_reasons["runbook"] = (
            "No solution implemented (Phase 5 skipped)"
        )
    
    # Post-Mortem (requires Phases 1-4 data)
    state.postmortem_available = (
        case.anomaly_frame is not None and
        case.timeline is not None and
        len(case.hypotheses) > 0 and
        case.diagnosis is not None
    )
    if not state.postmortem_available:
        missing = []
        if not case.anomaly_frame: missing.append("Phase 1 triage")
        if not case.timeline: missing.append("Phase 2 timeline")
        if not case.hypotheses: missing.append("Phase 3 hypotheses")
        if not case.diagnosis: missing.append("Phase 4 evidence")
        state.unavailable_reasons["postmortem"] = (
            f"Incomplete investigation (missing: {', '.join(missing)})"
        )
    
    # Consulting Summary (Phase 0→6 only)
    state.consulting_summary_available = (
        case.current_phase == 6 and
        case.anomaly_frame is None  # Never entered Phase 1 investigation
    )
    
    return state
```

**Document Requirements**:

| Document | Requires | Available For | Contains |
|----------|----------|---------------|----------|
| **Incident Report** | Phase 1 | All paths with Phase 1 | Problem statement, severity, impact, symptoms |
| **Runbook** | Phase 5 | URGENT, Full investigation | Solution steps, verification, rollback |
| **Post-Mortem** | Phases 1-4 | Full investigation, Historical | Complete analysis, root cause, lessons |
| **Consulting Summary** | Phase 0 only | Consulting-only path | Chat summary, quick resolution |

**Path-Specific Availability**:

| Path | Phases Executed | Available Documents |
|------|----------------|---------------------|
| **0→6** (Consulting) | Phase 0 | ✅ Consulting Summary |
| **0→1→2→5→6** (URGENT) | 1, 2, 5 | ✅ Incident Report<br>✅ Runbook |
| **0→1→2→3→4→5→6** (Full) | All | ✅ Incident Report<br>✅ Runbook<br>✅ Post-Mortem |
| **0→1→2→3→4→6** (Post-Mortem) | 1-4 | ✅ Incident Report<br>✅ Post-Mortem |

**User Communication Example (URGENT path)**:
```
Agent: "Entering Phase 6 - Documentation

Available Documents:
✅ Incident Report - Captures what happened and immediate resolution
✅ Runbook - Documents the rollback procedure

Not Available:
❌ Post-Mortem - Requires detailed root cause analysis (skipped for speed)

Since this was urgent, we focused on quick mitigation. The incident report
and runbook will help prevent recurrence.

Would you like me to generate the incident report?"
```

#### 6.2 State Freezing

When entering Phase 6, freeze cross-phase state for documentation:

```python
def freeze_state_at_phase_6(case: Case):
    """Freeze cross-phase state when entering Phase 6."""
    
    # Freeze working conclusion
    if case.working_conclusion:
        case.documentation.frozen_working_conclusion = json.dumps(
            case.working_conclusion.dict()
        )
    
    # Freeze progress metrics
    if case.progress_metrics:
        case.documentation.frozen_progress_metrics = case.progress_metrics.dict()
    
    # OODA becomes inactive
    case.ooda_active = False
    case.current_ooda_step = None
    
    # Assess documentation capabilities
    case.documentation.documentation_state = assess_documentation_capabilities(case)
```

**Exit Criteria**:
- Documentation generated (or user declines)
- Lessons learned captured
- Knowledge base enriched
- Case closed

**Final State**: `status = CaseStatus.CLOSED`

---

## 4. OODA Loop Integration

### 4.1 OODA Overview

**Purpose**: Tactical progress within strategic phases.

**OODA Activation** (v2.0 - NEW):

```python
def activate_ooda(case: Case):
    """
    Activate OODA loop when entering Phase 1.
    Called automatically during Phase 0→1 transition.
    """
    case.ooda_active = True
    case.current_ooda_iteration = 1
    case.current_ooda_step = OODAStep.OBSERVE
    
    # Start first iteration
    first_iteration = OODAIteration(
        iteration_number=1,
        phase=case.current_phase,
        started_at=datetime.now(timezone.utc),
    )
    case.ooda_iterations.append(first_iteration)

def deactivate_ooda(case: Case):
    """
    Deactivate OODA loop when entering Phase 6.
    Called automatically during Phase N→6 transition.
    """
    case.ooda_active = False
    case.current_ooda_step = None
    
    # Complete last iteration if incomplete
    if case.ooda_iterations and not case.ooda_iterations[-1].completed_at:
        case.ooda_iterations[-1].completed_at = datetime.now(timezone.utc)
```

**OODA Lifecycle**:
```
Phase 0: ooda_active = False
    ↓ (Phase 0→1 transition)
    activate_ooda()
    ↓
Phases 1-5: ooda_active = True
    ↓ (Phase N→6 transition)
    deactivate_ooda()
    ↓
Phase 6: ooda_active = False
```

**OODA = Observe, Orient, Decide, Act**

Each investigation phase (1-5) uses OODA loops to make incremental progress toward phase objectives.

```
Phase Objective (Strategic)
        ↓
    OODA Iteration 1 (Tactical)
        ↓
    OODA Iteration 2
        ↓
    OODA Iteration N
        ↓
Phase Objective Achieved → Transition
```

### 4.2 OODA Steps

| Step | Purpose | Example |
|------|---------|---------|
| **OBSERVE** | Gather data | User uploads log file, agent reads it |
| **ORIENT** | Analyze data | Agent processes file, extracts key info |
| **DECIDE** | Choose action | Agent determines next step |
| **ACT** | Execute | Agent asks question, suggests solution, transitions phase |

### 4.3 OODA Iteration Lifecycle

**Iteration Structure**:
```python
class OODAIteration:
    iteration_number: int
    phase: int                               # Which phase (1-5)
    
    # Steps completed
    observe_completed: bool
    orient_completed: bool
    decide_completed: bool
    act_completed: bool
    
    # Results
    decision_made: str
    action_taken: str
    outcome: str
    
    # Timing
    started_at: datetime
    completed_at: datetime
    
    # Progress
    progress_made: bool                      # Did we make meaningful progress?
    blocked: bool
    blocked_reason: Optional[str]
```

**Iteration Completion**:
```python
def is_ooda_iteration_complete(iteration: OODAIteration) -> bool:
    """Check if iteration completed all four steps."""
    return (
        iteration.observe_completed and
        iteration.orient_completed and
        iteration.decide_completed and
        iteration.act_completed
    )
```

**Phase Exit via OODA**:

A phase can exit when OODA made progress AND phase criteria met:

```python
def can_exit_phase_via_ooda(case: Case) -> bool:
    """
    Determine if phase can exit based on OODA progress.
    
    Combines:
    1. OODA made progress (not stuck)
    2. Phase-specific exit criteria met
    """
    
    if not case.ooda_active or not case.ooda_iterations:
        return False
    
    # Check last iteration made progress
    last_iteration = case.ooda_iterations[-1]
    if not last_iteration.progress_made:
        return False
    
    # Check phase-specific criteria
    if case.current_phase == 1:
        return case.anomaly_frame is not None and case.anomaly_frame.verified
    elif case.current_phase == 2:
        return len(case.timeline.events) >= 2
    elif case.current_phase == 3:
        return len(case.hypotheses) > 0
    elif case.current_phase == 4:
        return any(h.status == HypothesisStatus.VALIDATED for h in case.hypotheses.values())
    elif case.current_phase == 5:
        if case.investigation_strategy == InvestigationStrategy.URGENT:
            return case.solution_applied
        else:
            return case.solution_applied and case.solution_verified
    
    return False
```

**OODA Progress Criteria** (what counts as "progress"):

| Phase | Progress Made If... |
|-------|---------------------|
| **Phase 1** | New symptom verified, scope clarified, impact assessed |
| **Phase 2** | New event added, correlation detected, timeline narrowed |
| **Phase 3** | New hypothesis generated, hypothesis refined, requirements defined |
| **Phase 4** | Evidence collected, hypothesis validated/refuted, confidence changed |
| **Phase 5** | Solution proposed, solution applied, solution verified |

### 4.4 OODA Escalation

If 3+ iterations pass without progress, escalate:

```python
def should_escalate_ooda(case: Case) -> bool:
    """
    Escalate if 3+ iterations without progress.
    Indicates investigation stuck, needs intervention.
    """
    
    if case.current_ooda_iteration < 3:
        return False
    
    # Check last 3 iterations
    recent_iterations = case.ooda_iterations[-3:]
    no_progress = all(not iter.progress_made for iter in recent_iterations)
    
    if no_progress:
        case.escalation_state = EscalationState(
            escalated=True,
            escalation_reason=f"No progress in {case.current_ooda_iteration} OODA iterations",
            escalated_at=datetime.now(),
        )
        return True
    
    return False
```

**Escalation Triggers**:
- Human review
- Expert consultation
- Degraded mode entry (see Section 6)

**Example: OODA with Escalation**:
```
Phase 4: Testing hypothesis "Null pointer in auth code"

Iteration 1:
  OBSERVE: User uploads app.log
  ORIENT: No null pointers found
  DECIDE: Request auth-service logs
  ACT: "Please provide auth-service logs"
  Progress: False

Iteration 2:
  OBSERVE: User "I don't have access"
  ORIENT: User blocked
  DECIDE: Ask for alternative
  ACT: "Can you provide database logs?"
  Progress: False

Iteration 3:
  OBSERVE: User uploads database logs
  ORIENT: Logs don't show auth errors
  DECIDE: Evidence inconclusive
  ACT: "Check with ops team?"
  Progress: False

After Iteration 3:
  Escalation triggered
  Enter degraded mode (USER_BLOCKED)
  Fallback: Proceed with limited evidence
```

---

## 5. Cross-Phase State

### 5.1 Working Conclusion

**Purpose**: Track evolving understanding throughout investigation.

**Lifecycle**:
- **Phase 0**: NULL
- **Phases 1-5**: Active (updated continuously)
- **Phase 6**: Frozen (snapshot preserved)

**Structure**:
```python
class WorkingConclusion:
    # Core statement
    statement: str                           # Current understanding
    
    # Dual confidence tracking
    confidence: float                        # Numeric (0-1)
    confidence_level: ConfidenceLevel        # Categorical
    
    # Evidence basis
    supporting_evidence_count: int
    total_evidence_count: int
    evidence_completeness: float
    
    # Transparency
    caveats: List[str]                       # Known limitations
    alternative_explanations: List[str]      # Other possibilities
    
    # Action guidance
    can_proceed_with_solution: bool          # Ready for Phase 5?
    next_evidence_needed: List[str]          # What would help
    
    # Linkage
    validated_hypothesis_id: Optional[str]   # If validated
```

#### 5.1.1 Confidence Calibration

**Dual Tracking**: Numeric + Categorical

```python
class ConfidenceLevel(str, Enum):
    SPECULATION = "speculation"  # 0.0-0.3 (wild guess)
    PROBABLE = "probable"        # 0.3-0.6 (reasonable theory)
    CONFIDENT = "confident"      # 0.6-0.8 (strong evidence)
    VERIFIED = "verified"        # 0.8-1.0 (validated hypothesis)
```

**Mapping Function**:
```python
def determine_confidence_level(confidence: float) -> ConfidenceLevel:
    """Map numeric confidence to categorical level."""
    if confidence >= 0.8:
        return ConfidenceLevel.VERIFIED
    elif confidence >= 0.6:
        return ConfidenceLevel.CONFIDENT
    elif confidence >= 0.3:
        return ConfidenceLevel.PROBABLE
    else:
        return ConfidenceLevel.SPECULATION
```

**Solution Readiness Threshold**:

Requires **BOTH** conditions:
1. `confidence >= 0.7` (70%+ confidence)
2. `evidence_completeness >= 0.6` (60%+ requirements fulfilled)

```python
def can_proceed_with_solution(working_conclusion: WorkingConclusion) -> bool:
    """
    Determine if confident enough to implement solution.
    
    Rationale:
    - High confidence without evidence = risky (overconfident)
    - High evidence without confidence = unclear (contradictory)
    - Both required for safe solution implementation
    """
    return (
        working_conclusion.confidence >= 0.7 and
        working_conclusion.evidence_completeness >= 0.6
    )
```

**Confidence Update on New Evidence**:
```python
def update_working_conclusion_confidence(case: Case, new_evidence: Evidence):
    """
    Update confidence based on evidence stance.
    
    Adjustments:
    - STRONGLY_SUPPORTS: +0.15
    - SUPPORTS: +0.10
    - NEUTRAL: No change
    - CONTRADICTS: -0.10
    - STRONGLY_CONTRADICTS: -0.20
    """
    
    if not case.working_conclusion:
        return
    
    validated_hyp_id = case.working_conclusion.validated_hypothesis_id
    if not validated_hyp_id or validated_hyp_id not in new_evidence.stance:
        return
    
    stance = new_evidence.stance[validated_hyp_id]
    
    # Adjust confidence
    if stance == EvidenceStance.STRONGLY_SUPPORTS:
        case.working_conclusion.confidence = min(1.0, 
            case.working_conclusion.confidence + 0.15)
    elif stance == EvidenceStance.SUPPORTS:
        case.working_conclusion.confidence = min(1.0, 
            case.working_conclusion.confidence + 0.10)
    elif stance == EvidenceStance.CONTRADICTS:
        case.working_conclusion.confidence = max(0.0, 
            case.working_conclusion.confidence - 0.10)
    elif stance == EvidenceStance.STRONGLY_CONTRADICTS:
        case.working_conclusion.confidence = max(0.0, 
            case.working_conclusion.confidence - 0.20)
    
    # Update categorical level
    case.working_conclusion.confidence_level = determine_confidence_level(
        case.working_conclusion.confidence
    )
    
    # Update evidence basis
    if stance in [EvidenceStance.STRONGLY_SUPPORTS, EvidenceStance.SUPPORTS]:
        case.working_conclusion.supporting_evidence_count += 1
    case.working_conclusion.total_evidence_count += 1
    case.working_conclusion.evidence_completeness = (
        case.working_conclusion.supporting_evidence_count / 
        case.working_conclusion.total_evidence_count
    )
    
    # Update action guidance
    case.working_conclusion.can_proceed_with_solution = can_proceed_with_solution(
        case.working_conclusion
    )
```

**Example Confidence Evolution**:
```
Initial: Hypothesis "Null pointer in auth code"
  Confidence: 0.5 (PROBABLE)
  Evidence: 0/0

Evidence 1: Stack trace with null pointer
  Stance: STRONGLY_SUPPORTS
  New confidence: 0.5 + 0.15 = 0.65 (CONFIDENT)
  Evidence: 1/1 = 100%

Evidence 2: Code review confirms null pointer possible
  Stance: SUPPORTS
  New confidence: 0.65 + 0.10 = 0.75 (CONFIDENT)
  Evidence: 2/2 = 100%
  can_proceed_with_solution: True ✅ (0.75 >= 0.7 and 1.0 >= 0.6)

Evidence 3: Metrics show errors started BEFORE code change
  Stance: CONTRADICTS
  New confidence: 0.75 - 0.10 = 0.65 (CONFIDENT)
  Evidence: 2/3 = 67%
  can_proceed_with_solution: False ❌ (0.65 < 0.7)

Conclusion: Hypothesis refuted, need new hypothesis
```

### 5.2 Progress Metrics

**Purpose**: Track investigation progress and momentum.

**Lifecycle**: Same as WorkingConclusion (Active in 1-5, Frozen at 6)

**Structure**:
```python
class ProgressMetrics:
    # Evidence collection
    evidence_completeness: float             # % requirements fulfilled
    evidence_blocked_count: int              # Blocked requests
    evidence_pending_count: int              # Awaiting response
    evidence_complete_count: int             # Fulfilled requests
    
    # Investigation momentum
    investigation_momentum: InvestigationMomentum  # HIGH|MODERATE|LOW|BLOCKED
    turns_since_last_progress: int           # Turns without progress
    
    # Hypothesis progress
    active_hypotheses_count: int             # Being tested
    hypotheses_with_sufficient_evidence: int # Can decide
    highest_hypothesis_confidence: float     # Best hypothesis
    
    # Next steps
    next_steps: List[str]                    # Recommended actions
    blocked_reasons: List[str]               # What's blocking
```

**Investigation Momentum**:
```python
class InvestigationMomentum(str, Enum):
    HIGH = "high"          # Making rapid progress
    MODERATE = "moderate"  # Steady progress
    LOW = "low"           # Slow progress
    BLOCKED = "blocked"   # No progress, intervention needed
```

**Momentum Determination**:
```python
def determine_investigation_momentum(case: Case) -> InvestigationMomentum:
    """Determine current investigation velocity."""
    
    # BLOCKED: No progress in 5+ turns or explicitly blocked
    if case.progress_metrics.turns_since_last_progress > 5:
        return InvestigationMomentum.BLOCKED
    
    if case.degraded_mode is not None:
        return InvestigationMomentum.BLOCKED
    
    # HIGH: Recent progress, evidence flowing
    if (case.progress_metrics.turns_since_last_progress <= 2 and
        case.last_evidence_added_turn and
        case.current_turn - case.last_evidence_added_turn <= 2):
        return InvestigationMomentum.HIGH
    
    # LOW: Slow progress
    if case.progress_metrics.turns_since_last_progress > 3:
        return InvestigationMomentum.LOW
    
    # MODERATE: Default
    return InvestigationMomentum.MODERATE
```

### 5.3 Root Cause Conclusion

**Purpose**: Final root cause determination.

**When Created**: Phase 4, when hypothesis validated with high confidence

**Structure**:
```python
class RootCauseConclusion:
    validated_hypothesis_id: str             # Which hypothesis validated
    root_cause_statement: str                # Definitive explanation
    
    # Dual confidence
    confidence: float                        # Numeric (≥0.8 required)
    confidence_level: ConfidenceLevel        # VERIFIED
    
    # Evidence
    evidence_basis: str                      # Description of evidence
    supporting_evidence_ids: List[str]       # Evidence IDs
    
    # Multiple factors
    contributing_factors: List[str]          # Additional factors
    
    # Metadata
    determined_at: datetime
    determined_at_turn: int
    determined_by: str
```

**Synchronization with Working Conclusion**:

When root cause determined, **synchronize** with working conclusion:

```python
def create_root_cause_conclusion(
    case: Case,
    validated_hypothesis_id: str,
    root_cause_statement: str,
    confidence: float,  # Must be ≥0.8
    evidence_basis: str,
    supporting_evidence_ids: List[str],
    contributing_factors: List[str] = []
) -> RootCauseConclusion:
    """
    Create root cause conclusion and sync with working conclusion.
    
    Synchronization ensures single source of truth across case.
    """
    
    # Validate confidence
    if confidence < 0.8:
        raise ValueError(f"Root cause requires confidence ≥ 0.8, got {confidence}")
    
    # Create root cause
    case.root_cause_conclusion = RootCauseConclusion(
        validated_hypothesis_id=validated_hypothesis_id,
        root_cause_statement=root_cause_statement,
        confidence=confidence,
        confidence_level=ConfidenceLevel.VERIFIED,
        evidence_basis=evidence_basis,
        supporting_evidence_ids=supporting_evidence_ids,
        contributing_factors=contributing_factors,
        determined_at=datetime.now(timezone.utc),
        determined_at_turn=case.current_turn,
        determined_by=case.user_id,
    )
    
    # Synchronize working conclusion
    if case.working_conclusion:
        case.working_conclusion.statement = root_cause_statement
        case.working_conclusion.confidence = confidence
        case.working_conclusion.confidence_level = ConfidenceLevel.VERIFIED
        case.working_conclusion.can_proceed_with_solution = True
        case.working_conclusion.validated_hypothesis_id = validated_hypothesis_id
        case.working_conclusion.last_updated_turn = case.current_turn
        case.working_conclusion.last_confidence_change_turn = case.current_turn
    
    # Update hypothesis
    if validated_hypothesis_id in case.hypotheses:
        case.hypotheses[validated_hypothesis_id].status = HypothesisStatus.VALIDATED
        case.hypotheses[validated_hypothesis_id].likelihood = confidence
    
    return case.root_cause_conclusion
```

**Why Synchronization Matters**:

Without sync:
```
root_cause_conclusion.root_cause_statement = "Null pointer in auth code"
root_cause_conclusion.confidence = 0.95

working_conclusion.statement = "Unknown cause"
working_conclusion.confidence = 0.5

Result: Contradictory state, confusion
```

With sync:
```
Both reflect same understanding
Single source of truth
Consistent UI/API responses
```

---

## 6. Degraded Mode Handling

### 6.1 Philosophy

**Core Principle**: Partial investigation is better than no investigation.

Normal investigation assumes:
- User can provide requested data
- System can process data
- Investigation proceeds smoothly

When these assumptions fail, enter **degraded mode** with fallback strategies.

**Degraded mode enables**:
1. **Transparency**: Explain what's missing and why
2. **Fallback**: Offer alternative path forward
3. **Documentation**: Record blockers for future resolution
4. **Exit Path**: Clear conditions to resume normal mode

### 6.2 Degraded Mode Types

```python
class DegradedModeType(str, Enum):
    LIMITED_DATA = "limited_data"              # Insufficient data available
    EXTERNAL_DEPENDENCY = "external_dependency"  # Waiting on external team
    HYPOTHESIS_DEADLOCK = "hypothesis_deadlock"  # All hypotheses inconclusive
    USER_BLOCKED = "user_blocked"              # User can't provide needed info
    SYSTEM_LIMITATION = "system_limitation"    # Technical constraint
```

### 6.3 LIMITED_DATA Mode

**Trigger**: Required evidence cannot be obtained

```python
if (case.evidence_requests and 
    all(not req.fulfilled for req in case.evidence_requests) and
    case.current_ooda_iteration >= 3):
    enter_degraded_mode(case, DegradedModeType.LIMITED_DATA)
```

**Fallback Strategy**:
- Lower confidence thresholds (0.6 instead of 0.7)
- Proceed with best available evidence
- Document what's missing
- Make probabilistic recommendations
- Flag solution as "based on limited evidence"

**Exit Criteria**: New evidence becomes available

**Example**:
```
Phase 4: Testing hypothesis "Database locks causing timeouts"
  Request: "Please provide database lock traces"
  
User: "Don't have access to production database"

Agent: [Enters LIMITED_DATA mode]
  "I understand you don't have database access. Let me work with what we have.
  
  Based on limited evidence (application timeouts, no DB traces):
  - Confidence: 60% (lower than usual 70% due to limited data)
  - Recommendation: Likely database locks, but cannot confirm
  - Fallback: Let's check application-side connection pool metrics
  
  Missing evidence documented:
  - Database lock traces (production access required)
  - Query execution times (production access required)
  
  Shall we proceed with application metrics?"
```

### 6.4 EXTERNAL_DEPENDENCY Mode

**Trigger**: Need information from external team

```python
if user_message_contains(["need to ask", "waiting on", "another team"]):
    enter_degraded_mode(case, DegradedModeType.EXTERNAL_DEPENDENCY)
```

**Fallback Strategy**:
- Document blocker and who to follow up with
- Suggest parallel investigation paths
- Set reminder to check back

**Exit Criteria**: External team responds

**Example**:
```
Phase 2: Building timeline

User: "I need the DBA team to check for long-running queries"

Agent: [Enters EXTERNAL_DEPENDENCY mode]
  "Understood - we need DBA team input for complete timeline.
  
  External Dependency Recorded:
  - Team: Database Administration (DBA)
  - Information: Long-running queries during incident
  - Follow-up: john.smith@company.com
  
  While waiting:
  - We can investigate application-side timeouts
  - Check connection pool metrics
  - Analyze application logs for retry patterns
  
  I'll flag this for follow-up when DBA responds.
  Shall we proceed with application-side investigation?"
```

### 6.5 HYPOTHESIS_DEADLOCK Mode

**Trigger**: All hypotheses marked INCONCLUSIVE

```python
if all(h.status == HypothesisStatus.INCONCLUSIVE for h in case.hypotheses.values()):
    enter_degraded_mode(case, DegradedModeType.HYPOTHESIS_DEADLOCK)
```

**Fallback Strategy**:
- Broaden hypothesis generation (consider edge cases)
- Generate alternative theories
- Consider less common causes
- Escalate to expert review

**Exit Criteria**: New hypothesis generated or expert engaged

**Example**:
```
Phase 4: All hypotheses tested

Hypothesis 1: "DB connection pool exhausted" → INCONCLUSIVE
Hypothesis 2: "Memory leak" → INCONCLUSIVE  
Hypothesis 3: "Network latency" → INCONCLUSIVE

Agent: [Enters HYPOTHESIS_DEADLOCK mode]
  "All primary hypotheses inconclusive. May be looking at less common cause.
  
  Hypothesis Deadlock Analysis:
  - 3 hypotheses tested, all inconclusive
  - Evidence doesn't strongly support or refute any
  
  Fallback Options:
  1. Generate alternatives (less common causes):
     - Race condition in request handling
     - Third-party API intermittent failures
     - Clock skew causing authentication issues
  
  2. Escalate to senior engineer
  
  3. Proceed with probabilistic diagnosis
  
  Which approach would you prefer?"
```

### 6.6 USER_BLOCKED Mode

**Trigger**: User cannot access required systems/data

```python
if user_message_contains(["don't have access", "can't access", "no permissions"]):
    enter_degraded_mode(case, DegradedModeType.USER_BLOCKED)
```

**Fallback Strategy**:
- Identify accessible alternatives
- Suggest indirect evidence
- Document access requirements
- Provide steps to request access

**Exit Criteria**: User gains access or provides alternatives

**Example**:
```
Phase 4: Collecting evidence

Agent: "Please provide Kubernetes pod logs from production"

User: "I don't have kubectl access to production"

Agent: [Enters USER_BLOCKED mode]
  "I understand you don't have production kubectl access.
  
  Access Blocker Documented:
  - Required: kubectl access to production cluster
  - To request: Contact DevOps team (devops@company.com)
  
  Alternative Evidence You CAN Provide:
  1. Monitoring dashboard screenshots (Grafana access?)
  2. Application logs (centralized in Splunk?)
  3. PagerDuty alerts from incident window
  4. Slack messages from #production-alerts
  
  These won't be as complete as pod logs, but still valuable.
  
  Would you like to proceed with alternatives?"
```

### 6.7 SYSTEM_LIMITATION Mode

**Trigger**: Technical limitation prevents normal flow

```python
if technical_error_occurred or cannot_process_file:
    enter_degraded_mode(case, DegradedModeType.SYSTEM_LIMITATION)
```

**Fallback Strategy**:
- Explain limitation clearly
- Provide manual analysis steps
- Suggest workarounds
- Document limitation for product team

**Exit Criteria**: Limitation resolved or workaround found

**Example**:
```
Phase 2: User uploads 5GB log file

System: Cannot process (size limit: 500MB)

Agent: [Enters SYSTEM_LIMITATION mode]
  "I encountered a system limitation with your log file.
  
  Technical Limitation:
  - File size: 5.0 GB
  - System limit: 500 MB
  - Reason: Processing exceeds memory constraints
  
  Workaround Options:
  1. Filter logs to incident time window:
     grep '2025-11-01 14:2[0-9]' app.log > filtered.log
     
  2. Split file by time period:
     Split into hourly chunks
     
  3. Provide summary manually:
     grep ERROR app.log | wc -l
     Tell me error count and sample errors
  
  I've documented this for the product team.
  Which workaround would you prefer?"
```

### 6.8 Degraded Mode State Management

```python
def enter_degraded_mode(
    case: Case, 
    mode_type: DegradedModeType, 
    reason: str, 
    fallback: str
):
    """
    Enter degraded mode with fallback strategy.
    
    Updates:
    - Sets degraded_mode field
    - Updates progress metrics (momentum = BLOCKED)
    - Records blocker reasons
    - Notifies user
    """
    
    case.degraded_mode = DegradedMode(
        mode_type=mode_type,
        entered_at_turn=case.current_turn,
        reason=reason,
        fallback_strategy=fallback,
        exit_criteria=get_exit_criteria(mode_type),
    )
    
    case.progress_metrics.investigation_momentum = InvestigationMomentum.BLOCKED
    case.progress_metrics.blocked_reasons.append(reason)
    
    notify_user_degraded_mode(case)

def exit_degraded_mode(case: Case, resolution: str):
    """
    Exit degraded mode when conditions met.
    
    Updates:
    - Clears degraded_mode
    - Restores momentum
    - Clears blockers
    - Notifies user
    """
    
    if case.degraded_mode:
        case.degraded_mode.exited_at_turn = case.current_turn
        
        case.progress_metrics.investigation_momentum = InvestigationMomentum.MODERATE
        case.progress_metrics.blocked_reasons.clear()
        
        case.degraded_mode = None
        
        notify_user_degraded_mode_exit(case, resolution)
```

**Decision Logic**:
```python
def should_enter_degraded_mode(case: Case) -> Optional[DegradedModeType]:
    """Determine if should enter degraded mode and which type."""
    
    # Check LIMITED_DATA
    if (len(case.evidence_requests) > 0 and
        all(not req.fulfilled for req in case.evidence_requests) and
        case.current_ooda_iteration >= 3):
        return DegradedModeType.LIMITED_DATA
    
    # Check HYPOTHESIS_DEADLOCK
    if (len(case.hypotheses) > 0 and
        all(h.status == HypothesisStatus.INCONCLUSIVE 
            for h in case.hypotheses.values())):
        return DegradedModeType.HYPOTHESIS_DEADLOCK
    
    # Check general blockage (USER_BLOCKED)
    if (case.progress_metrics.investigation_momentum == InvestigationMomentum.BLOCKED and
        case.progress_metrics.turns_since_last_progress > 5):
        return DegradedModeType.USER_BLOCKED
    
    return None
```

---

## 7. State Management & Transitions

### 7.1 Case Status

```python
class CaseStatus(str, Enum):
    CONSULTING = "consulting"          # Phase 0 - problem exploration
    INVESTIGATING = "investigating"    # Phases 1-5 - active investigation
    RESOLVED = "resolved"             # Solution verified, awaiting closure
    CLOSED = "closed"                 # Phase 6 complete
```

**Status Transitions**:
```
CONSULTING ──(start investigation)──> INVESTIGATING
    │                                       │
    │                                       │
    └──(no investigation)──> CLOSED         ├──(solution applied & verified)──> RESOLVED
                                           │
                                           └──(user closes)──> CLOSED
                                           
RESOLVED ──(enter Phase 6)──> CLOSED
```

### 7.2 Phase Transition Rules

**Summary Table**:

| From | To | Requires |
|------|-----|----------|
| **0→1** | Phase 1 | `problem_confirmation` exists AND `decided_to_investigate = True` |
| **1→2** | Phase 2 | `anomaly_frame` exists AND `verified = True` |
| **2→3** | Phase 3 | `timeline` exists AND `len(events) >= 2` |
| **2→5** | Phase 5 | URGENT routing: `correlation_confidence > 0.9` + CRITICAL urgency |
| **3→4** | Phase 4 | `len(hypotheses) > 0` |
| **4→5** | Phase 5 | At least one hypothesis VALIDATED OR root_cause_conclusion exists |
| **5→6** | Phase 6 | URGENT: `solution_applied` / NON_URGENT: `solution_applied + verified` |
| **Any→6** | Phase 6 | User can manually close (USER_FORCED) |

### 7.3 Transition Tracking

**Every transition recorded**:
```python
class PhaseTransition:
    from_phase: int
    to_phase: int
    triggered_by_source: UpdateSource          # USER | SYSTEM
    triggered_by: str                         # User email or "system"
    reason: str                               # Why transition occurred
    timestamp: datetime
    is_loop_back: bool                        # True if backwards (to_phase < from_phase)
```

**Loop-Back Tracking**:

If user wants to go backwards (e.g., Phase 4 → Phase 3):
```python
class LoopBackEvent:
    from_phase: int
    to_phase: int
    reason: str
    occurred_at: datetime
    triggered_by: str

# Track loop-back count
case.loop_back_count += 1

# Escalate if too many loop-backs
if case.loop_back_count > case.max_loop_backs:
    escalate_case(case, "Excessive loop-backs (user uncertain)")
```

### 7.4 Solution Tracking

**Case-Level Flags**:
```python
case.solution_applied: bool              # Has solution been applied?
case.solution_verified: bool             # Has it been verified?
case.minutes_since_solution: int         # Time since application
```

**Used for**:
- Phase 5→6 transition criteria
- Progress monitoring
- Verification reminders

### 7.5 Evidence Tracking

**Case-Level Tracking**:
```python
case.evidence_requests: List[EvidenceRequest]    # Active requests
case.evidence_provided: List[str]                # Evidence IDs provided
case.last_evidence_added_turn: int               # When last evidence added
```

**Used for**:
- Progress metrics calculation
- Detecting when investigation stalled
- Reminder to provide evidence

### 7.6 Turn Tracking

**Turn = One complete user-agent exchange**

```python
case.current_turn: int                   # Current turn number
```

**Every significant event records turn**:
- Phase transitions: `phase_updated_at_turn`
- Evidence added: `collected_at_turn`
- Hypothesis created: `created_at_turn`
- OODA iterations: tracked per turn

**Used for**:
- Detecting staleness
- Progress velocity calculation
- Anchoring detection

### 7.7 Escalation State

```python
class EscalationState:
    escalated: bool
    escalation_reason: str
    escalated_at: datetime
    escalated_to: str                    # Email of person escalated to
    resolution_notes: Optional[str]
```

**Escalation Triggers**:
- 3+ OODA iterations without progress
- 3+ loop-backs
- User explicitly requests escalation
- System detects investigation stuck

---

## 8. Examples & Scenarios

### 8.1 URGENT Path (Production Incident)

```
[Phase 0: User Reports Issue]
User: "Our website is completely down! All users getting 502 errors!"

Agent: "That sounds critical. Let me help investigate immediately.
       Can you tell me when this started?"
       
User: "About 10 minutes ago, 2:15 PM"

Agent: [Creates problem_confirmation with CRITICAL urgency]
      "I'll help you investigate this formally. Let's start."
      [User implicitly confirms → decided_to_investigate = True]

[Phase 1: Triage]
Agent: "Let's confirm the scope. Is this affecting all users globally?"

User: "Yes, total outage"

Agent: [Creates AnomalyFrame]
      "Confirmed: Total outage, all users, 502 errors since 14:15 UTC
       Severity: CRITICAL
       Can you upload recent deployment logs or error logs?"
       
User: [Uploads deployment logs + error logs]

[Phase 2: Timeline]
Agent: [Extracts events]
      "Timeline constructed:
       - 14:13 UTC: Deployment v3.2.1 started
       - 14:14 UTC: Deployment completed
       - 14:15 UTC: 502 errors began (100% of requests)
       
       Strong correlation detected (confidence: 0.95)
       Deployment immediately preceded errors.
       
       RECOMMENDATION: URGENT path - rollback deployment"

[Skip to Phase 5]
Agent: "Immediate solution: Rollback to v3.2.0
       This will restore service immediately."
       
User: "Rolling back now... Deployment rolled back."
      [solution_applied = True]

Agent: "Monitoring error rates... dropping to 0%. Service restored!
       
       Let's document this incident."

[Phase 6: Documentation]
Agent: [Generates incident report and rollback runbook]
      
      "Case closed. Total time: 25 minutes.
       
       Next steps:
       - Investigate why v3.2.1 caused 502 errors
       - Fix bug before redeploying
       - Review deployment testing procedures"
```

**Timeline**: 25 minutes  
**Phases**: 0→1→2→5→6  
**Documents**: Incident Report, Rollback Runbook

---

### 8.2 NON_URGENT Path (Complex Investigation)

```
[Phase 0: User Reports Issue]
User: "We've been seeing intermittent timeouts on our API for the past week.
       About 2-3% of requests timeout after 30 seconds."

Agent: "I can help investigate this. Can you tell me more about the pattern?
       Is it affecting all endpoints or specific ones?"
       
User: "Seems to be mostly the /users endpoint"

Agent: [Creates problem_confirmation]
      "This sounds like it needs formal investigation to find the root cause.
       Would you like me to help investigate this thoroughly?"
       
User: "Yes please"
      [decided_to_investigate = True]

[Phase 1: Triage]
Agent: "Let's define the problem clearly.
       - Issue: Timeouts on /users endpoint
       - Frequency: 2-3% of requests
       - Duration: Past week
       - Impact: Some user requests failing
       
       Can you upload logs or metrics showing the timeouts?"
       
User: [Uploads application logs + Grafana metrics]

Agent: [Verifies with data]
      [Creates verified AnomalyFrame]
      "Confirmed: /users endpoint timing out 2.5% of requests
       Severity: MEDIUM (not urgent, but needs investigation)"

[Phase 2: Timeline]
Agent: "When did this start exactly? Was there any change around that time?"

User: "Started around October 25th. We did deploy v2.8.0 on October 24th."

Agent: [Builds timeline]
      "Timeline:
       - Oct 24, 14:00: Deployed v2.8.0
       - Oct 25, 09:00: First timeouts observed
       - Oct 25-present: Timeouts continue at 2-3%
       
       Correlation confidence: 0.65 (moderate)
       
       Could be related to deployment, but timing is not exact.
       Let's investigate thoroughly with hypotheses."

[Phase 3: Hypothesis Generation]
Agent: "Let me generate some hypotheses about what could cause this:
       
       Hypothesis 1: Database connection pool too small
       - Rationale: Timeouts suggest resource exhaustion
       - Evidence needed: Connection pool metrics, database logs
       
       Hypothesis 2: Memory leak in user service
       - Rationale: Gradual degradation over time
       - Evidence needed: Memory usage trends, heap dumps
       
       Hypothesis 3: Increased load from new feature
       - Rationale: v2.8.0 may have introduced new queries
       - Evidence needed: Query counts, endpoint usage metrics
       
       Let's test these systematically."

[Phase 4: Diagnosis]
Agent: "Let's start with Hypothesis 1. Can you provide:
       - Database connection pool metrics from the past week
       - Database slow query logs"
       
User: [Uploads connection pool metrics]

Agent: [Analyzes evidence]
      "Connection pool metrics show:
       - Pool size: 10 connections
       - Peak usage: 9-10 connections (90-100%)
       - Wait times: Up to 30 seconds when pool full
       
       This STRONGLY SUPPORTS Hypothesis 1.
       Hypothesis 1 likelihood increased to 0.85 (CONFIDENT)
       
       Can you also provide slow query logs to confirm?"
       
User: [Uploads database logs]

Agent: "Slow query logs show:
       - Most queries complete in <100ms
       - No problematic slow queries
       - Confirms issue is pool exhaustion, not query performance
       
       Hypothesis 1 VALIDATED (confidence: 0.90)
       Root cause: Database connection pool size too small"

[Phase 5: Solution]
Agent: "Root cause identified: Connection pool exhausted
       
       Immediate solution:
       1. Restart service to clear stuck connections
       
       Longterm solution:
       2. Increase connection pool size from 10 to 50
       3. Add connection pool monitoring alerts
       
       Let's start with the restart."
       
User: "Service restarted. Timeouts stopped immediately."
      [solution_applied = True]

Agent: "Great! Now let's implement the permanent fix.
       Can you update the connection pool configuration?"
       
User: "Updated config, increased pool to 50, deployed v2.8.1"

Agent: "Excellent. Let's verify with 24 hours of monitoring.
       I'll check back tomorrow."
       
[Next day]
User: "No timeouts in the past 24 hours!"

Agent: [solution_verified = True]
      "Perfect! Solution verified. Let's document this."

[Phase 6: Documentation]
Agent: [Generates incident report, root cause analysis, and runbook]
      
      "Case closed. Total time: 2 days.
       
       Summary:
       - Root cause: Database connection pool too small (10 connections)
       - Solution: Increased pool to 50 connections
       - Verified: 24 hours with 0% timeouts
       
       Lessons learned:
       - Monitor connection pool usage proactively
       - Set alerts at 70% pool usage
       - Review pool sizing for new deployments"
```

**Timeline**: 2 days  
**Phases**: 0→1→2→3→4→5→6  
**Documents**: Incident Report, Root Cause Analysis, Runbook

---

### 8.3 Consulting-Only Path (No Investigation)

```
[Phase 0: User Question]
User: "Is it normal for my Redis cache hit rate to be around 60%?"

Agent: "A 60% cache hit rate can be normal or concerning depending on context.
       
       Typical cache hit rates:
       - 80-95%: Excellent (most requests served from cache)
       - 60-80%: Moderate (acceptable for many use cases)
       - <60%: Low (may indicate cache sizing or TTL issues)
       
       Your 60% is on the moderate-to-low side.
       
       To improve it:
       - Increase cache size if memory available
       - Extend TTL for stable data
       - Review which keys are being cached
       - Check for cache invalidation patterns
       
       Would you like to investigate this formally to find specific optimization opportunities?"

User: "No, just wanted to check if it was reasonable. Thanks!"

Agent: "You're welcome! Your cache hit rate is acceptable, but there's room for improvement if you want to optimize later."
      [decided_to_investigate = False]
      [Transition directly to Phase 6]

[Phase 6: Documentation]
Agent: [Generates consulting summary]
      
      "Consulting Summary:
       - Question: Cache hit rate of 60% normal?
       - Answer: Moderate-to-low, acceptable but improvable
       - Suggestions: Increase size, extend TTL, review invalidation
       - Status: No formal investigation needed"
```

**Timeline**: 5 minutes  
**Phases**: 0→6  
**Documents**: Consulting Summary

---

### 8.4 Degraded Mode (USER_BLOCKED)

```
[Phase 4: Evidence Collection]
Agent: "To test Hypothesis 1 (database locks), please provide:
       - Database lock trace logs from production
       - Query execution time metrics"
       
User: "I don't have access to the production database.
       Only the DBA team has that access."

Agent: [Enters USER_BLOCKED degraded mode]
      "I understand you don't have production database access.
       
       Access Blocker Documented:
       - Required: Production database access
       - To request: Contact DBA team (dba@company.com)
       - Alternative owner: Sarah Chen (DB admin)
       
       Alternative Evidence You CAN Provide:
       1. Application-side connection pool metrics
       2. Application timeout logs
       3. Monitoring dashboard screenshots
       4. Recent database maintenance schedules
       
       These won't show database internals, but can still help us
       narrow down the issue from the application side.
       
       Would you like to proceed with application-side evidence?"

User: "Yes, I can provide application metrics."

Agent: "Great! Please upload:
       - Connection pool usage metrics
       - Application timeout logs
       
       I'll also document the missing database access for follow-up."
       
User: [Uploads application metrics]

Agent: [Analyzes limited evidence]
      "Based on application metrics (without DB traces):
       
       Findings:
       - Connection pool hitting 100% capacity during timeouts
       - Wait times correlating with timeout errors
       - No application-level errors (issue downstream)
       
       Conclusion (60% confidence - lower due to limited data):
       - LIKELY: Database locks or slow queries
       - CANNOT CONFIRM without database traces
       
       Recommendation:
       1. Immediate: Increase connection pool size (25% → 50%)
          This will mitigate timeout impact regardless of root cause
          
       2. Follow-up: Request database lock traces from DBA team
          for complete diagnosis
       
       Shall we proceed with the connection pool increase?"

[Exit degraded mode when DBA provides data OR user accepts limited-confidence solution]
```

---

## 9. Persistence and Storage Architecture

### 9.1 Core Principle

**Investigation state belongs to CASE (permanent), not SESSION (temporary).**

This aligns with the architectural principle from `case-and-session-concepts.md`:
- **Cases** are top-level resources (permanent, independent)
- **Sessions** are for authentication only (temporary, client-based)  
- **Investigation state persists with case**, accessible across all user sessions

### 9.2 Redis Key Design

#### Investigation State Storage

```
Redis Key: investigation:{case_id}  ✅ CORRECT (NOT session_id!)
Value: JSON-serialized InvestigationState
TTL: 2592000 seconds (30 days) - matches case TTL
Storage Engine: Redis
```

**Why case_id, not session_id?**

| Design | Key | TTL | Consequence |
|--------|-----|-----|-------------|
| ❌ **WRONG** | `investigation:{session_id}` | 24 hours | **Data loss**: Investigation state deleted when session expires. User cannot resume investigation next day. Multi-device broken. |
| ✅ **CORRECT** | `investigation:{case_id}` | 30 days | **Persistent**: Investigation survives session expiry. Multi-device access works. User can resume anytime within 30 days. |

#### Case Data Storage

```
Redis Key: case:{case_id}
Value: JSON-serialized Case object
TTL: 2592000 seconds (30 days)
Storage Engine: Redis
```

### 9.3 InvestigationState Structure

The `InvestigationState` object stored in Redis contains:

```python
class InvestigationState(BaseModel):
    """
    Complete investigation runtime state.
    Stored in Redis as: investigation:{case_id}
    """
    
    # Investigation metadata
    metadata: InvestigationMetadata  # investigation_id, user_id, timestamps
    
    # Lifecycle tracking
    lifecycle: InvestigationLifecycle  # current_phase, case_status, phase_history
    
    # OODA engine state
    ooda_engine: OODAEngineState  # anomaly_frame, temporal_frame, hypotheses
    
    # Cross-phase state
    working_conclusion: Optional[WorkingConclusion]
    progress_metrics: Optional[ProgressMetrics]
    root_cause_conclusion: Optional[RootCauseConclusion]
    
    # Hierarchical memory
    memory: MemoryState
    
    # Evidence tracking
    evidence_requests: List[EvidenceRequest]
    evidence_status: EvidenceCompletionStatus
```

### 9.4 Storage Operations

#### Initialize Investigation

```python
async def initialize_investigation(case_id: str, user_id: str):
    """
    Initialize investigation state for a case.
    
    CRITICAL: Keyed by case_id, NOT session_id!
    """
    investigation_state = InvestigationState(
        metadata=InvestigationMetadata(
            investigation_id=f"inv_{case_id}",  # Use case_id!
            user_id=user_id,
            started_at=datetime.now(timezone.utc),
        ),
        lifecycle=InvestigationLifecycle(
            current_phase=InvestigationPhase.INTAKE,
            case_status="consulting",
        ),
    )
    
    # Store with case_id as key
    key = f"investigation:{case_id}"
    ttl = 30 * 24 * 60 * 60  # 30 days
    
    await redis.set(key, investigation_state.json(), ex=ttl)
```

#### Retrieve Investigation State

```python
async def get_investigation_state(case_id: str) -> Optional[InvestigationState]:
    """
    Retrieve investigation state for a case.
    
    Args:
        case_id: Case identifier (NOT session_id!)
    """
    key = f"investigation:{case_id}"
    state_json = await redis.get(key)
    
    if not state_json:
        return None
    
    return InvestigationState.parse_raw(state_json)
```

#### Update Investigation State

```python
async def update_investigation_state(
    case_id: str,
    state: InvestigationState
):
    """
    Update investigation state for a case.
    
    Args:
        case_id: Case identifier (NOT session_id!)
        state: Updated InvestigationState object
    """
    key = f"investigation:{case_id}"
    ttl = 30 * 24 * 60 * 60  # 30 days
    
    await redis.set(key, state.json(), ex=ttl)
```

### 9.5 Multi-Session Architecture

**Key Benefit**: Investigation state persists across sessions and devices.

```
User's Investigation Journey:

Day 1, Laptop Session:
├─ Session: session_laptop_abc (24h TTL)
├─ Case: case_123 (30d TTL)
└─ Investigation: investigation:case_123 (30d TTL)

Day 2, Laptop Session Expired:
├─ Session: session_laptop_abc ❌ EXPIRED
├─ Case: case_123 ✅ Still exists
└─ Investigation: investigation:case_123 ✅ Still exists!

Day 2, Mobile Session:
├─ Session: session_mobile_xyz (NEW, 24h TTL)
├─ Case: case_123 ✅ Same case
└─ Investigation: investigation:case_123 ✅ Resume from Day 1 state!
```

### 9.6 Authorization Flow

Investigation state access is authorized through case ownership:

```python
async def access_investigation(case_id: str, session_id: str):
    """
    Access investigation with proper authorization.
    
    Authorization Chain:
    Session → User → Case → Investigation
    """
    # 1. Authenticate: Get user from session
    user_id = await session_store.get_user_id(session_id)
    
    # 2. Authorize: Verify case ownership
    case = await case_store.get_case(case_id)
    if case.owner_id != user_id:
        raise Unauthorized("User does not own this case")
    
    # 3. Retrieve: Load investigation state (keyed by case_id)
    investigation = await get_investigation_state(case_id)
    
    return investigation
```

### 9.7 TTL Management

**Investigation State TTL**: 30 days (matches case TTL)
- Investigation and case have **synchronized lifecycles**
- Both expire together after 30 days of inactivity
- Any case update resets both TTLs

**Session TTL**: 24 hours (INDEPENDENT from case/investigation)
- Sessions are temporary authentication tokens
- Session expiry does **NOT** affect investigation state
- User can create new session and resume investigation

**Cleanup Strategy**:
```python
# When case is deleted
async def delete_case(case_id: str):
    # Delete case data
    await redis.delete(f"case:{case_id}")
    
    # Delete investigation state (same lifecycle)
    await redis.delete(f"investigation:{case_id}")
    
# When session expires (DO NOT delete investigation!)
async def cleanup_expired_sessions():
    # Only delete session data
    await redis.delete(f"session:{session_id}")
    
    # Investigation state remains intact (tied to case, not session)
```

### 9.8 State Synchronization

**Case ↔ Investigation State Synchronization**:

Case model maintains minimal sync layer (`Case.diagnostic_state`):
- `has_active_problem`: Synced from `InvestigationState.engagement_mode`
- `current_phase`: Synced from `InvestigationState.lifecycle.current_phase`
- `investigation_state_id`: Points to `investigation:{case_id}` key

**Synchronization happens during**:
- Phase transitions
- OODA iteration completion
- Status changes

```python
async def sync_case_with_investigation(case_id: str):
    """Sync Case object with InvestigationState"""
    investigation = await get_investigation_state(case_id)
    case = await case_store.get_case(case_id)
    
    # Update Case sync fields
    case.current_phase = investigation.lifecycle.current_phase
    case.has_active_problem = (
        investigation.metadata.engagement_mode == EngagementMode.INVESTIGATOR
    )
    
    await case_store.update_case(case)
```

---

## Summary

### Framework Principles

1. **Phase-Based Structure**: Clear progression through 7 phases (0-6)
2. **Adaptive Routing**: URGENT vs NON_URGENT paths based on urgency
3. **OODA Loops**: Tactical progress within strategic phases
4. **Cross-Phase State**: Working conclusion and progress metrics track investigation
5. **Resilience**: Degraded mode enables progress when blocked
6. **Bidirectional**: Framework prescribes, Case data informs

### Key Decision Points

1. **Phase 0→1**: Requires problem confirmation AND user decision
2. **Phase 2 Routing**: correlation_confidence > 0.9 + CRITICAL → URGENT path
3. **Phase 4 Validation**: evidence_ratio > 0.7 + completeness > 0.6 → Validated
4. **Phase 5→6**: URGENT: solution_applied / NON_URGENT: solution_applied + verified
5. **Degraded Mode**: 3+ OODA iterations without progress → Enter degraded mode

### Confidence Calibration

- **0.0-0.3**: SPECULATION (wild guess)
- **0.3-0.6**: PROBABLE (reasonable theory)
- **0.6-0.8**: CONFIDENT (strong evidence)
- **0.8-1.0**: VERIFIED (validated hypothesis)
- **Solution threshold**: 0.7 confidence + 0.6 evidence completeness

### Complete Investigation Paths

| Path | Route | Timeline | Use Case |
|------|-------|----------|----------|
| **Consulting-Only** | 0→6 | Minutes | Quick Q&A |
| **URGENT** | 0→1→2→5→6 | Hours | Production incident |
| **NON_URGENT** | 0→1→2→3→4→5→6 | Days | Complex root cause |
| **Post-Mortem** | 0→1→2→3→4→6 | Days | Historical analysis |

---

**Document Version**: 2.0  
**Last Updated**: 2025-11-02  
**Status**: Complete - Fully Integrated Framework with Routing Logic Fixed  
**Authors**: System Architecture Team

**Changes from v1.0**:
1. ✅ Integrated all 10 updates from Framework Updates v1.1
2. ✅ Added Section 2.3: Phase 0→1 transition requires user decision
3. ✅ Added Section 3.2: Correlation confidence calculation
4. ✅ Added Section 3.3: Hypothesis generation modes (OPPORTUNISTIC/SYSTEMATIC/FORCED_ALTERNATIVE)
5. ✅ Added Section 3.4.1: Evidence request lifecycle management
6. ✅ Added Section 3.5.1: Solution type logic (URGENT vs NON_URGENT)
7. ✅ Added Section 3.6.1: Documentation capability assessment
8. ✅ Added Section 4.3: OODA iteration exit criteria
9. ✅ Added Section 5.1.1: Confidence threshold mappings and calibration
10. ✅ Added Section 5.3: Root cause synchronization with working conclusion
11. ✅ Added Section 6: Complete degraded mode handling (NEW)
12. ✅ Expanded all examples with complete workflows
13. ✅ Added comprehensive examples section (Section 8)

**Critical Routing Fixes** (Post-v1.1 Integration):
14. ✅ Fixed Phase 1 exit criteria: Added confidence >= 0.6 and urgency_level checks
15. ✅ Added Phase 2 investigation strategy determination logic (complete_phase_2)
16. ✅ Added Phase 3 entry guard documentation (blocks URGENT path)
17. ✅ Added Phase 4 entry guard documentation (blocks URGENT path)
18. ✅ Clarified Phase 5 dual entry point validation (from Phase 2 OR Phase 4)
19. ✅ Added OODA activation/deactivation logic (Section 4.1)
20. ✅ Added HypothesisStatus.INCONCLUSIVE for degraded mode support

**Final Validation Round** (Post-Routing Fixes):
21. ✅ Phase 4→6 post-mortem exit path documented in exit criteria
22. ✅ Fixed consulting_summary_available field reference (use anomaly_frame=None check)
23. ✅ Added routing_confirmed_by_user safety check in complete_phase_2()

**Result**: Single, complete, standalone reference document with 100% Case Data Model harmony AND fully correct adaptive routing logic (all 4 paths validated).