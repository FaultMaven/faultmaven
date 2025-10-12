# Evidence Collection and Tracking Design
## Data Models and Behavioral Specification v2.1

**Document Type:** Component Specification (Evidence Layer)
**Version:** 2.1
**Last Updated:** 2025-10-11
**Status:** ✅ **IMPLEMENTED** (v3.2.0)
**Parent Framework:** [Investigation Phases and OODA Integration Framework](./investigation-phases-and-ooda-integration.md)

## Implementation Status

**Implementation Date:** 2025-10-11
**Implementation Version:** v3.2.0
**Status:** Integrated with OODA framework

**Implementation Summary:**
- ✅ EvidenceRequest model with AcquisitionGuidance implemented
- ✅ Evidence Layer integrated into InvestigationState hierarchy
- ✅ 5-dimensional classification schemas defined (models/evidence.py)
- ✅ Phase-specific evidence request generation in all phase handlers
- ✅ Evidence tracking across OODA iterations
- ✅ ProblemConfirmation used in Phase 0 (Intake)
- ✅ Investigation strategies (Active Incident, Post-Mortem) implemented

**Implementation Files:**
- Evidence Models: `faultmaven/models/evidence.py`
- Evidence Layer: `faultmaven/models/investigation.py` (EvidenceLayer)
- Phase Handlers: `faultmaven/services/agentic/phase_handlers/*.py` (evidence requests)
- Strategy Selector: `faultmaven/core/investigation/strategy_selector.py`

**Note:** Evidence collection schemas from this design are now used by phase handlers during OODA Observe steps to generate structured evidence requests.

---

## Document Scope and Authority

### What This Document Covers (Authoritative)

This document is the **authoritative specification** for evidence collection data models and behaviors:

**Data Models** (Complete Schemas):
- EvidenceRequest with AcquisitionGuidance (7 categories, 5 states)
- EvidenceProvided with FileMetadata and classification
- ProblemConfirmation (Phase 0 output)
- Evidence lifecycle state transitions

**Investigation Strategies** (Behavioral Specifications):
- Active Incident Strategy (speed, mitigation-first approach)
- Post-Mortem Strategy (thoroughness, complete RCA approach)
- Strategy selection logic and mode-specific agent prompts

**Evidence Classification**:
- 5-dimensional classification system (request matching, completeness, form, type, intent)
- Multi-dimensional analysis algorithms
- Completeness scoring and tracking

**Safety and Validation**:
- Command safety rules and dangerous pattern detection
- Input validation and sanitization
- Safe evidence acquisition guidance

**Agent Behaviors**:
- Evidence request generation prompt templates
- Classification prompts and workflows
- Strategy-specific agent instructions
- Refuting evidence confirmation workflow

### Framework Integration

This design implements the **Evidence Layer** of the InvestigationState hierarchy, as defined in the [Investigation Phases and OODA Integration Framework](./investigation-phases-and-ooda-integration.md).

**Investigation Phase Context**:
- **Phase 0 (Intake)**: Creates ProblemConfirmation structure
- **Phase 1 (Blast Radius)**: First evidence requests generated (symptoms, scope)
- **Phase 2 (Timeline)**: Timeline and changes evidence collection
- **Phase 3 (Hypothesis)**: Evidence to support hypothesis generation
- **Phase 4 (Validation)**: Targeted evidence for hypothesis testing
- **Phase 5 (Solution)**: Solution verification evidence
- **Phase 6 (Document)**: Synthesis only, no new evidence collection

**OODA Integration**:
- **Observe Step**: Generates EvidenceRequests using schemas from this document
- **Orient Step**: Classifies provided evidence using 5-dimensional classification
- **Evidence Tracking**: Persists in InvestigationState.evidence_layer across OODA iterations

See parent framework for investigation phase definitions, OODA step integration, and when/why evidence collection occurs.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Design Principles](#design-principles)
3. [Core Concepts](#core-concepts)
4. [Data Models](#data-models)
5. [Multi-Dimensional Evidence Classification](#multi-dimensional-evidence-classification)
6. [Investigation Strategies](#investigation-strategies)
7. [Agent Behavior Specification](#agent-behavior-specification)
8. [Evidence Lifecycle Management](#evidence-lifecycle-management)
9. [Case Status State Machine](#case-status-state-machine)
10. [User Interaction Flows](#user-interaction-flows)
11. [Safety and Validation](#safety-and-validation)
12. [API Contracts](#api-contracts)
13. [Examples and Reference Implementations](#examples-and-reference-implementations)

---

## System Overview

### Purpose

This specification defines how FaultMaven collects, tracks, and manages diagnostic evidence throughout the investigation lifecycle. The system guides users through systematic problem diagnosis by requesting specific diagnostic evidence with actionable acquisition instructions, tracking evidence collection state across conversation turns, and adapting collection strategy based on incident urgency.

### Design Philosophy

Evidence collection is designed around **"Evidence Over Questions"** principle:
- Request specific diagnostic data with commands/paths, not abstract questions
- Provide actionable acquisition guidance (how to obtain evidence)
- Track evidence lifecycle (pending → partial → complete/blocked)
- Handle user agency (may deviate, provide unsolicited data, or report unavailable)

### Key Capabilities

1. **Evidence Request Generation**: Agents generate structured requests with acquisition guidance
2. **Multi-Dimensional Classification**: Classify user input across 5 dimensions
3. **Evidence Lifecycle Tracking**: Persistent state across conversation turns
4. **Dual-Mode Operation**: Different strategies for active incidents vs post-incident analysis
5. **Problem Confirmation**: Explicit confirmation before investigation begins
6. **Confidence Scoring**: Quantified confidence in root cause conclusions
7. **Post-Resolution Artifacts**: Automated generation of case reports and runbooks
8. **Graceful Degradation**: Alternative investigation paths when evidence unavailable

---

## Design Principles

### 1. Evidence Over Questions

**Principle**: Request specific diagnostic data with instructions, not questions for the user to answer.

**Rationale**: Users need actionable guidance. Asking "When did this start?" requires user to formulate answer. Requesting "Timeline evidence: check monitoring dashboard for error spike, or run `journalctl --since='1 hour ago'`" provides clear action.

**Implementation**: Every evidence request includes acquisition guidance with commands, file paths, and UI locations.

---

### 2. User Agency with Agent Guidance

**Principle**: User may deviate, ignore requests, or provide unsolicited data. Agent must handle gracefully while maintaining diagnostic agenda.

**Rationale**: Users are not obligated to follow agent's plan. Agent acts as consultant, not controller.

**Implementation**:
- Multi-dimensional classification handles any user input
- Agent always answers user questions even if off-topic
- Evidence requests persist but don't block conversation
- Alternative paths generated when evidence blocked

---

### 3. Context Preservation Across Turns

**Principle**: Evidence requests and provided evidence persist as case state, not regenerated each turn.

**Rationale**: Prevents re-requesting satisfied evidence. Maintains investigation continuity.

**Implementation**:
- Evidence requests stored in `CaseDiagnosticState.evidence_requests`
- Evidence provided stored in `CaseDiagnosticState.evidence_provided`
- Status updates (pending → partial → complete) tracked per request

---

### 4. Mode-Based Investigation Strategy

**Principle**: Active incidents prioritize speed and mitigation; post-mortems prioritize depth and learning.

**Rationale**: Different situations require different approaches. Service-down urgency differs from historical analysis.

**Implementation**:
- Mode detection based on problem statement and urgency
- Agent prompts adjust methodology (fast path vs thorough path)
- Escalation triggers differ by mode
- Confidence scoring required for post-mortem conclusions

---

### 5. Safety by Default

**Principle**: Never suggest commands that could cause data loss or security breach.

**Rationale**: Users may execute suggested commands without full understanding.

**Implementation**:
- Command validation against dangerous patterns
- Read-only commands preferred
- Warn about sudo requirements
- Alternative suggestions over risky commands

---

### 6. Explicit State Transitions

**Principle**: Investigation state changes (problem confirmation, phase advancement, resolution) require explicit signals.

**Rationale**: Prevents premature conclusions or missed steps.

**Implementation**:
- Problem definition requires user confirmation before investigation
- Phase advancement based on objective completion
- Resolution requires user validation of fix

---

## Core Concepts

### Evidence Request

A structured request for specific diagnostic data, including:
- **What**: Type of evidence needed (error logs, timeline, configuration)
- **Why**: How it aids diagnosis (rationale)
- **How**: Acquisition instructions (commands, paths, alternatives)
- **Status**: Lifecycle state (pending, partial, complete, blocked, obsolete)

### Evidence Classification

User input classified across five dimensions:
1. **Request Matching**: Which evidence requests it addresses (0 to N)
2. **Completeness**: Degree of satisfaction (partial/complete/over-complete)
3. **Form**: Text input vs file upload
4. **Type**: Supportive/refuting/neutral/absence
5. **Intent**: Providing evidence vs asking question vs reporting unavailable

### Investigation Strategy

When Lead Investigator Mode is activated (see [Investigation Phases Framework - Engagement Modes](./investigation-phases-and-ooda-integration.md#engagement-mode-system)), the system selects one of two investigation strategies:

- **Active Incident Strategy**: Service down, prioritize mitigation, fast hypothesis testing
- **Post-Mortem Strategy**: Historical analysis, prioritize thoroughness, multiple hypotheses

Strategy determines agent behavior, evidence prioritization, OODA iteration intensity, and escalation triggers.

### Case Status

Case lifecycle status tracks investigation progress independent of investigation phases.

**7 Case Statuses**:
- **INTAKE**: Gathering initial information (Phase 0)
- **IN_PROGRESS**: Active investigation (Phases 1-5)
- **MITIGATED**: Service restored but root cause unknown (Active Incident only)
- **RESOLVED**: Root cause found and fixed (Phase 5 complete)
- **STALLED**: Investigation blocked (evidence unavailable, hypotheses exhausted)
- **ABANDONED**: User disengaged
- **CLOSED**: Formally closed (Phase 6 complete or earlier termination)

**Complete Specification**: See [Case Lifecycle Management](./case-lifecycle-management.md) for:
- Complete state machine with transition rules
- Stall detection algorithm
- Status-based behaviors
- API integration

**Note**: Case Status is orthogonal to Investigation Phases. A case can be `IN_PROGRESS` while progressing through Phases 1-5.

---

## Data Models

### EvidenceRequest

```python
class EvidenceRequest(BaseModel):
    """Structured request for diagnostic evidence"""

    # Identity and metadata
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    label: str = Field(..., max_length=100, description="Brief title")
    description: str = Field(..., max_length=500, description="What's needed and why")
    category: EvidenceCategory

    # Acquisition guidance
    guidance: AcquisitionGuidance

    # Lifecycle tracking
    status: EvidenceStatus = EvidenceStatus.PENDING
    created_at_turn: int
    updated_at_turn: Optional[int] = None

    # Completeness tracking
    completeness: float = Field(0.0, ge=0.0, le=1.0)

    # Metadata
    metadata: Dict[str, Any] = Field(default_factory=dict)


class EvidenceCategory(str, Enum):
    """Categories of diagnostic evidence"""
    SYMPTOMS = "symptoms"              # Error manifestations
    TIMELINE = "timeline"              # When issue started/changed
    CHANGES = "changes"                # Recent deployments, config changes
    CONFIGURATION = "configuration"    # System/app configuration
    SCOPE = "scope"                    # Impact radius (users, services)
    METRICS = "metrics"                # Performance data
    ENVIRONMENT = "environment"        # Infrastructure state


class EvidenceStatus(str, Enum):
    """Lifecycle states of evidence request"""
    PENDING = "pending"        # Requested, awaiting user
    PARTIAL = "partial"        # Some information provided, need more
    COMPLETE = "complete"      # Fully satisfied
    BLOCKED = "blocked"        # User cannot provide (access/unavailable)
    OBSOLETE = "obsolete"      # No longer relevant to investigation
```

### AcquisitionGuidance

```python
class AcquisitionGuidance(BaseModel):
    """Instructions for obtaining evidence"""

    commands: List[str] = Field(
        default_factory=list,
        max_items=3,
        description="Shell commands to execute (read-only preferred)"
    )

    file_locations: List[str] = Field(
        default_factory=list,
        max_items=3,
        description="File system paths to check"
    )

    ui_locations: List[str] = Field(
        default_factory=list,
        max_items=3,
        description="UI navigation paths (Dashboard > Logs > Errors)"
    )

    alternatives: List[str] = Field(
        default_factory=list,
        max_items=3,
        description="Alternative methods (Docker: docker logs, K8s: kubectl logs)"
    )

    prerequisites: List[str] = Field(
        default_factory=list,
        max_items=2,
        description="Access requirements (sudo, admin role, etc.)"
    )

    expected_output: Optional[str] = Field(
        None,
        max_length=200,
        description="What to look for in results"
    )
```

### EvidenceProvided

```python
class FileMetadata(BaseModel):
    """Metadata for uploaded files (documents, log excerpts)"""
    filename: str
    content_type: str = Field(..., description="MIME type (e.g., text/plain, application/json)")
    size_bytes: int
    upload_timestamp: datetime
    file_id: str = Field(..., description="Reference to stored file")


class EvidenceProvided(BaseModel):
    """Record of evidence user provided"""

    evidence_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    turn_number: int
    timestamp: datetime

    # Content
    form: EvidenceForm
    content: str = Field(..., description="Text or file reference/path")
    file_metadata: Optional[FileMetadata] = None  # Populated when form == DOCUMENT

    # Classification
    addresses_requests: List[str] = Field(
        default_factory=list,
        description="Evidence request IDs this satisfies"
    )
    completeness: CompletenessLevel
    evidence_type: EvidenceType
    user_intent: UserIntent

    # Analysis
    key_findings: List[str] = Field(default_factory=list)
    confidence_impact: Optional[float] = None


class EvidenceForm(str, Enum):
    USER_INPUT = "user_input"    # Text via query/ endpoint
    DOCUMENT = "document"        # File via data/ endpoint (includes log excerpts)


class EvidenceType(str, Enum):
    """Evidential value"""
    SUPPORTIVE = "supportive"    # Confirms hypothesis or direction
    REFUTING = "refuting"        # Contradicts hypothesis
    NEUTRAL = "neutral"          # No clear support or contradiction
    ABSENCE = "absence"          # Checked but evidence not found


class CompletenessLevel(str, Enum):
    """Describes how well evidence answers specific request(s)"""
    PARTIAL = "partial"              # 0.3-0.7: Some info, need more
    COMPLETE = "complete"            # 0.8-1.0: Fully answers request
    OVER_COMPLETE = "over_complete"  # Satisfies >1 request (len(matched_request_ids) > 1)
```

### EvidenceClassification

```python
class EvidenceClassification(BaseModel):
    """Multi-dimensional classification of user input"""

    # Dimension 1: Request matching
    matched_request_ids: List[str] = Field(default_factory=list)

    # Dimension 2: Completeness
    completeness: CompletenessLevel
    completeness_score: float = Field(ge=0.0, le=1.0)

    # Dimension 3: Form
    form: EvidenceForm

    # Dimension 4: Type
    evidence_type: EvidenceType

    # Dimension 5: Intent
    user_intent: UserIntent

    # Reasoning
    rationale: str
    follow_up_needed: Optional[str] = None


class UserIntent(str, Enum):
    """User's intention with input"""
    PROVIDING_EVIDENCE = "providing_evidence"
    ASKING_QUESTION = "asking_question"
    REPORTING_UNAVAILABLE = "reporting_unavailable"
    REPORTING_STATUS = "reporting_status"        # "Working on getting logs"
    CLARIFYING = "clarifying"                    # "What do you mean by..."
    OFF_TOPIC = "off_topic"
```

### InvestigationStrategy and CaseStatus

```python
class InvestigationStrategy(str, Enum):
    """Investigation approach - selected when Lead Investigator Mode activated"""
    ACTIVE_INCIDENT = "active_incident"    # Service down, speed priority
    POST_MORTEM = "post_mortem"            # Historical analysis, thoroughness priority


class CaseStatus(str, Enum):
    """Case lifecycle status"""
    INTAKE = "intake"
    IN_PROGRESS = "in_progress"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    STALLED = "stalled"
    ABANDONED = "abandoned"
    CLOSED = "closed"
```

### CaseDiagnosticState Extensions

```python
class CaseDiagnosticState(BaseModel):
    """Extended diagnostic state with evidence tracking"""

    # Existing fields (from v1.0)
    current_phase: int = 0
    has_active_problem: bool = False
    problem_statement: Optional[str] = None
    urgency_level: UrgencyLevel = UrgencyLevel.NORMAL

    # Investigation mode
    investigation_mode: InvestigationMode = InvestigationMode.ACTIVE_INCIDENT
    case_status: CaseStatus = CaseStatus.INTAKE

    # Evidence tracking
    evidence_requests: List[EvidenceRequest] = Field(default_factory=list)
    evidence_provided: List[EvidenceProvided] = Field(default_factory=list)

    # Incident management
    incident_mitigated: bool = False
    mitigation_turn: Optional[int] = None
    mitigation_actions: List[str] = Field(default_factory=list)

    # Investigation state
    investigation_dead_ends: List[DeadEnd] = Field(default_factory=list)
    stall_reason: Optional[str] = None

    # Problem confirmation
    problem_confirmed: bool = False
    problem_confirmation: Optional[ProblemConfirmation] = None

    # Resolution
    root_cause_conclusion: Optional[RootCauseConclusion] = None
    resolution_artifacts: Optional[ResolutionArtifacts] = None


class DeadEnd(BaseModel):
    """Investigated path ruled out"""
    hypothesis: str
    evidence_checked: List[str]
    why_ruled_out: str
    turn_number: int
    confidence_eliminated: float = Field(ge=0.0, le=1.0)


class ProblemConfirmation(BaseModel):
    """
    Initial problem triage structure - created in Phase 0 (Intake)
    
    This is DISTINCT from AnomalyFrame (Phase 1):
    - ProblemConfirmation: Informal, consent gate, created in Consultant Mode
    - AnomalyFrame: Formal, structured, created in Lead Investigator Mode
    
    Workflow:
    1. Phase 0: Agent creates ProblemConfirmation
    2. User consents → Mode switch to Lead Investigator
    3. Phase 1: Agent creates AnomalyFrame (formal definition)
    
    See Investigation Phases Framework for AnomalyFrame schema.
    """
    problem_statement: str  # User's description
    affected_components: List[str]  # Approximate components
    severity: str  # Initial assessment
    impact: str  # Who/what affected
    investigation_approach: str  # Proposed strategy
    estimated_evidence_needed: List[str]  # Expected evidence categories


class RootCauseConclusion(BaseModel):
    """Root cause determination with confidence"""
    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence: List[str]
    missing_evidence: List[str]
    alternative_explanations: List[Dict[str, Any]] = Field(default_factory=list)
    caveats: List[str] = Field(default_factory=list)


class ResolutionArtifacts(BaseModel):
    """Post-resolution deliverables"""
    case_report_offered: bool = False
    case_report_generated: Optional[str] = None
    runbook_offered: bool = False
    runbook_exists: bool = False
    runbook_generated: Optional[str] = None
```

---

## Multi-Dimensional Evidence Classification

### Classification Algorithm

```python
async def classify_evidence(
    user_input: str,
    active_requests: List[EvidenceRequest],
    conversation_history: List[Message],
    llm_client: LLMClient
) -> EvidenceClassification:
    """
    Classify user input across five dimensions using LLM.

    Returns classification determining:
    1. Which evidence requests (if any) this addresses
    2. Completeness level
    3. Form (text vs file)
    4. Evidential type (supportive/refuting/neutral/absence)
    5. User intent
    """
```

### Classification Prompt Structure

```
CONTEXT:
Active evidence requests:
{formatted_requests}

User input:
{user_input}

CLASSIFY across 5 dimensions:

1. REQUEST MATCHING
   Which request IDs does this address? (can be multiple, or none)
   Consider semantic similarity, not just keywords.

2. COMPLETENESS
   - partial (0.3-0.7): Some information, but incomplete
   - complete (0.8-1.0): Fully answers this specific request

   NOTE: "over_complete" means the user provided evidence that satisfies
   MULTIPLE evidence requests simultaneously. This is reflected by the
   matched_request_ids list containing >1 request, NOT by a score >1.0.
   Each request maintains its own 0.0-1.0 completeness score.

3. FORM
   - user_input: Text entered by user
   - document: File upload

4. EVIDENCE TYPE
   - supportive: Confirms/supports investigation direction
   - refuting: Contradicts hypothesis or expectation
   - neutral: Doesn't clearly support or contradict
   - absence: User checked but evidence doesn't exist

5. USER INTENT
   - providing_evidence: User answering evidence request
   - asking_question: User asking for clarification/info
   - reporting_unavailable: User cannot provide evidence
   - reporting_status: Update on progress ("working on it")
   - clarifying: User asking what we mean
   - off_topic: Unrelated to investigation

OUTPUT JSON:
{
  "matched_request_ids": ["req-001"],
  "completeness": "complete",
  "completeness_score": 0.9,
  "form": "user_input",
  "evidence_type": "supportive",
  "user_intent": "providing_evidence",
  "rationale": "User provided error count matching request",
  "follow_up_needed": null
}
```

### Classification Decision Rules

1. **Multiple Matches Allowed**: User input may address 2+ requests simultaneously
2. **Absent Evidence is Valid**: User reporting "no errors in logs" is valuable (absence type)
3. **Intent Determines Handling**: Same text classified differently based on intent
4. **Ambiguity Acceptable**: When unclear, trust conversation flow over strict matching

---

## Investigation Strategies

### Strategy Selection

When **Lead Investigator Engagement Mode** is activated (after user consents in Phase 0), the system selects an investigation strategy based on urgency and problem timing.

```python
def select_investigation_strategy(
    urgency_level: UrgencyLevel,
    problem_statement: str,
    has_active_problem: bool,
    incident_mitigated: bool
) -> InvestigationStrategy:
    """
    Select investigation strategy based on urgency and timing.
    
    Called after Phase 0 (Intake) when Lead Investigator Mode activated.
    See Investigation Phases Framework for engagement mode definitions.
    """

    # Check for post-mortem indicators
    problem = problem_statement.lower() if problem_statement else ""
    post_mortem_indicators = [
        "what caused", "root cause of", "why did",
        "happened yesterday", "last week", "after the incident",
        "retrospective", "post-mortem", "rca"
    ]

    if any(indicator in problem for indicator in post_mortem_indicators):
        return InvestigationStrategy.POST_MORTEM

    # Check for active incident
    if (urgency_level in [UrgencyLevel.HIGH, UrgencyLevel.CRITICAL]
        and has_active_problem
        and not incident_mitigated):
        return InvestigationStrategy.ACTIVE_INCIDENT

    # Default to post-mortem (safer, more thorough)
    return InvestigationStrategy.POST_MORTEM
```

### Strategy Transition

```python
# ACTIVE_INCIDENT → POST_MORTEM transition (service restored)
if strategy == InvestigationStrategy.ACTIVE_INCIDENT:
    if user_confirms_service_restored():
        state.incident_mitigated = True
        state.mitigation_turn = current_turn
        state.investigation_strategy = InvestigationStrategy.POST_MORTEM
        state.case_status = CaseStatus.MITIGATED  # See case-lifecycle-management.md
```

### Strategy-Specific Behavior

| Aspect | Active Incident | Post-Mortem |
|--------|----------------|-------------|
| **Objective** | Restore service quickly | Understand root cause completely |
| **Evidence Priority** | Symptoms first, detailed analysis later | Comprehensive evidence collection |
| **Hypothesis Testing** | Test mitigation immediately | Validate with multiple data points |
| **Phase Advancement** | Can skip phases for speed | Complete all phases |
| **Escalation Threshold** | 3 failed attempts or critical evidence blocked | Hypotheses exhausted after thorough investigation |
| **Confidence Requirement** | Not required (mitigation over certainty) | Required with explicit score |
| **Time Pressure** | Minutes/hours matter | Days acceptable |
| **Acceptable Outcomes** | Service restored (even without root cause), Workaround applied, Escalation | Definitive root cause, Probable cause with confidence, "Cannot determine" with caveats |

---

## Agent Behavior Specification

### Evidence Request Generation

#### Requirements

1. **Quantity**: 2-3 requests per turn (unless phase complete)
2. **Specificity**: Clear, actionable descriptions
3. **Guidance Completeness**: Include commands, files, UI paths, alternatives
4. **Safety**: Only read-only commands; validate against dangerous patterns
5. **Constraint Adherence**: Max 3 items per guidance category

#### Prompt Template

```
EVIDENCE REQUEST GENERATION

Generate 2-3 evidence_requests in this EXACT JSON structure:

{
  "evidence_requests": [
    {
      "label": "Brief descriptive title (max 100 chars)",
      "description": "What's needed and why it helps diagnosis (max 500 chars)",
      "category": "symptoms|timeline|changes|configuration|scope|metrics|environment",
      "guidance": {
        "commands": ["cmd1", "cmd2"],  // Max 3, read-only preferred
        "file_locations": ["/path1", "/path2"],  // Max 3
        "ui_locations": ["Dashboard > Section"],  // Max 3
        "alternatives": ["If X, try Y"],  // Max 3
        "expected_output": "What to look for"  // Max 200 chars
      }
    }
  ]
}

SAFETY RULES:
❌ NEVER suggest: rm, chmod 777, curl|bash, /etc/passwd, DROP TABLE
✅ PREFER: tail, grep, cat, ls, ps, curl -X GET

CONSTRAINTS:
- Max 3 commands, 3 file paths, 3 UI locations, 3 alternatives
- Commands must be safe (read-only)
- Include why evidence is needed (rationale in description)
- Provide alternatives for different environments (Docker, K8s, cloud)

EXAMPLES:

Good:
{
  "label": "Application error logs",
  "description": "Need recent error logs to identify exact error messages and stack traces causing 500 responses",
  "category": "symptoms",
  "guidance": {
    "commands": [
      "tail -100 /var/log/app.log",
      "grep 'ERROR' /var/log/app.log | tail -50"
    ],
    "file_locations": ["/var/log/app.log", "~/logs/application.log"],
    "ui_locations": ["Monitoring Dashboard > Logs > Application"],
    "alternatives": [
      "Docker: docker logs <container-id> --tail 100",
      "Kubernetes: kubectl logs deployment/app --tail=100"
    ],
    "expected_output": "Lines with 'ERROR', '500', exception names, or stack traces"
  }
}

Bad:
{
  "label": "Check logs",  // Too vague
  "description": "Look at logs",  // No rationale
  "guidance": {
    "commands": ["rm -rf /var/log/*"],  // DANGEROUS!
    "file_locations": [],  // Missing
    "alternatives": []  // Missing
  }
}
```

### Strategy-Specific Prompt Additions

#### Active Incident Strategy

```
INVESTIGATION STRATEGY: ACTIVE_INCIDENT

URGENCY-DRIVEN APPROACH:
1. GOAL: Mitigation first, root cause later
2. EVIDENCE: Prioritize symptoms and recent changes
3. HYPOTHESIS: Form quick hypothesis, test mitigation immediately
4. VALIDATION: Confirm service restored (full validation later)
5. ESCALATION: Recommend after 3 failed attempts or critical evidence blocked

EVIDENCE REQUEST FOCUS:
- What's broken RIGHT NOW?
- What changed recently? (deployments, configs)
- How to stop the bleeding? (restart, rollback, scale)

RESPONSE PATTERN:
"I see {problem} affecting {scope} (CRITICAL). Let's stabilize:
1. {quick_mitigation_option_1}
2. {quick_mitigation_option_2}

Evidence needed for quick diagnosis:
{evidence_requests_with_guidance}

Root cause analysis can wait - service restoration first."

ESCALATION CRITERIA:
- Critical evidence blocked (no logs access, no deploy history)
- 3 mitigation attempts failed
- Symptoms worsening despite actions
```

#### Post-Mortem Strategy

```
INVESTIGATION STRATEGY: POST_MORTEM

DEPTH-DRIVEN APPROACH:
1. GOAL: Complete root cause understanding
2. EVIDENCE: Comprehensive collection, follow all leads
3. HYPOTHESIS: Multiple hypotheses, test systematically
4. VALIDATION: Require strong evidence for conclusions
5. CONFIDENCE: Explicit scoring (0.0-1.0) with supporting/missing evidence

EVIDENCE REQUEST FOCUS:
- Complete symptom picture (what happened)
- Detailed timeline (when, sequence of events)
- All changes in relevant time window
- System state during incident

RESPONSE PATTERN:
"Let's understand what caused {problem}. Since service is stable, we can investigate thoroughly.

Evidence needed for root cause analysis:
{evidence_requests_with_guidance}

If some data is unavailable (e.g., logs rotated), we'll work with what we have and identify the most probable cause."

CONFIDENCE SCORING:
When concluding root cause, provide:
- Root cause statement
- Confidence score (0.0-1.0)
- Supporting evidence list
- Missing evidence list
- Alternative explanations (if any)
- Caveats/assumptions

CONFIDENCE RUBRIC:
0.9-1.0: Direct evidence confirms (logs + metrics + timing align perfectly)
0.7-0.89: Strong supporting evidence, minor gaps
0.5-0.69: Circumstantial evidence, notable gaps
0.3-0.49: Speculative, significant unknowns
0.0-0.29: Educated guess, major gaps
```

## Problem Definition Stages

FaultMaven uses **two-stage problem definition** to transition from informal triage to formal investigation:

### Stage 1: ProblemConfirmation (Phase 0 - Intake)

**Purpose**: Initial problem triage and investigation consent gate  
**Engagement Mode**: Consultant  
**OODA Status**: Inactive  
**Created When**: User signals potential problem, before investigation starts

**Workflow**:
1. Agent synthesizes initial understanding from user conversation
2. Creates ProblemConfirmation structure (informal)
3. Presents to user for consent
4. User confirms → Mode switch to Lead Investigator, advance to Phase 1

**Agent Prompt Specification**:

```
PHASE 0 - PROBLEM CONFIRMATION (Consultant Mode)

After gathering initial information (typically 2-3 turns), you MUST:

1. SYNTHESIZE problem definition:
   - What is broken?
   - Who/what is affected?
   - How severe is it?
   - Investigation approach needed

2. FORMAT confirmation request:
   "Let me confirm my understanding:

   🎯 Problem: {concise_problem_statement}
   🔴 Impact: {affected_users_or_services}
   ⚠️ Severity: {critical|high|medium|low}

   To investigate this, I'll need to:
   - {evidence_type_1}
   - {evidence_type_2}
   - {evidence_type_3}

   Does this match what you're experiencing? Shall I start a formal investigation?"

3. WAIT for user confirmation:
   - If "yes" or affirmative → Set phase_complete=true, advance to Phase 1
   - If "no" or corrections → Update understanding, ask again
   - If unclear → Request clarification

4. DO NOT advance to Phase 1 without explicit user consent

OUTPUT STRUCTURE:
{
  "answer": "confirmation request text",
  "problem_confirmation": {
    "problem_statement": "...",
    "affected_components": [...],
    "severity": "...",
    "impact": "...",
    "investigation_approach": "...",
    "estimated_evidence_needed": [...]
  },
  "awaiting_confirmation": true,
  "phase_complete": false
}
```

---

### Stage 2: AnomalyFrame (Phase 1 - Blast Radius)

**Purpose**: Formal problem framing for systematic investigation  
**Engagement Mode**: Lead Investigator  
**OODA Status**: Active (Orient step output)  
**Created When**: After user consents, in Phase 1 (Blast Radius) via OODA Orient

**Workflow**:
1. OODA Observe: Gather structured facts (symptoms, scope, severity)
2. OODA Orient: Create formal AnomalyFrame
3. User confirms framing is accurate
4. AnomalyFrame guides evidence collection in subsequent phases

**Structure**: Defined in [Investigation Phases Framework - AnomalyFrame](./investigation-phases-and-ooda-integration.md#anomalyframe)

**Key Difference from ProblemConfirmation**:
- More structured and precise fields
- Based on evidence gathered, not just user report
- Revisable as investigation progresses (has confidence and revision tracking)
- Includes timeline anchor (started_at timestamp)
- Used as investigation blueprint in Phases 2-5

---

### Post-Resolution Deliverables

**SolutionAgent Specification:**

```
POST-RESOLUTION PROTOCOL:

When user confirms issue is RESOLVED:

1. CHECK if runbook exists for this issue type:
   Query knowledge base for similar root causes
   If similarity > 0.85 → runbook_exists = true

2. OFFER deliverables:
   "Great! The issue is resolved. Would you like me to generate:

   📋 Case Report - Detailed investigation summary:
      • Problem description and timeline
      • Evidence collected
      • Root cause identified (confidence: {score})
      • Solution applied and validated
      • Investigation dead ends (ruled out)
      • Lessons learned

   {if not runbook_exists}
   📖 Runbook - Step-by-step guide for future occurrences:
      • Issue identification (symptoms, detection)
      • Quick diagnosis steps
      • Resolution procedure (mitigation + permanent fix)
      • Prevention measures
      • Escalation contacts
   {endif}

   Would you like me to generate these? (yes/both/report only/runbook only/no)"

3. GENERATE based on user choice:
   - Case report: Markdown format, comprehensive
   - Runbook: Structured template, actionable
   - Save to knowledge base for future reference

4. UPDATE case status:
   RESOLVED → CLOSED
```

---

## Evidence Lifecycle Management

### Lifecycle States

```
PENDING ──[user provides]──> PARTIAL ──[user provides more]──> COMPLETE
   │                                                              │
   │                                                              │
   └──[user cannot provide]──> BLOCKED                          │
   │                                                              │
   └──[investigation moves on]──> OBSOLETE <────────────────────┘
```

### State Transition Rules

1. **PENDING → PARTIAL**
   - Trigger: User provides some but not all requested information
   - Completeness score: 0.3-0.7
   - Action: Keep request active, update description with what's still needed

2. **PENDING/PARTIAL → COMPLETE**
   - Trigger: User provides sufficient information
   - Completeness score: ≥ 0.8
   - Action: Mark complete, remove from active list

3. **PENDING → BLOCKED**
   - Trigger: User reports cannot access (permission, availability)
   - Action: Mark blocked, generate alternative investigation path

4. **ANY → OBSOLETE**
   - Trigger: Investigation progresses past need for this evidence
   - Action: Mark obsolete, remove from display

### Update Algorithm

```python
def update_evidence_lifecycle(
    classification: EvidenceClassification,
    diagnostic_state: CaseDiagnosticState,
    current_turn: int
) -> None:
    """Update evidence request states based on new input"""

    # Record evidence provided
    evidence_record = EvidenceProvided(
        turn_number=current_turn,
        form=classification.form,
        content=user_input,
        addresses_requests=classification.matched_request_ids,
        completeness=classification.completeness,
        evidence_type=classification.evidence_type,
        user_intent=classification.user_intent
    )
    diagnostic_state.evidence_provided.append(evidence_record)

    # Update matched requests
    for req_id in classification.matched_request_ids:
        request = find_request(diagnostic_state.evidence_requests, req_id)

        # Update completeness (use max, not accumulation)
        # Rationale: Each piece of evidence is independently scored against the request
        # The highest score reflects true completeness, not sum of partial contributions
        request.completeness = max(request.completeness, classification.completeness_score)

        # Update status
        if request.completeness >= 0.8:
            request.status = EvidenceStatus.COMPLETE
        elif request.completeness >= 0.3:
            request.status = EvidenceStatus.PARTIAL

        request.updated_at_turn = current_turn

    # Handle blocking
    if classification.user_intent == UserIntent.REPORTING_UNAVAILABLE:
        for req_id in classification.matched_request_ids:
            request = find_request(diagnostic_state.evidence_requests, req_id)
            request.status = EvidenceStatus.BLOCKED
            request.metadata["blocked_reason"] = user_input

    # Mark obsolete requests (agent decision in next turn)
```

---

## Case Status Integration

### Status Model Reference

Case status tracking is defined in a separate specification. See [Case Lifecycle Management](./case-lifecycle-management.md) for:
- Complete CaseStatus enum (7 states)
- State transition rules and diagram
- Stall detection algorithm
- Status-based behaviors

### Status Usage in Evidence Collection

**Investigation Strategy Selection**:
```python
# Case status influences strategy selection
if case_status == CaseStatus.MITIGATED:
    # Service already restored, always use POST_MORTEM for RCA
    strategy = InvestigationStrategy.POST_MORTEM
elif urgency == CRITICAL and case_status == CaseStatus.IN_PROGRESS:
    # Active critical issue
    strategy = InvestigationStrategy.ACTIVE_INCIDENT
else:
    # Normal investigation or post-incident
    strategy = InvestigationStrategy.POST_MORTEM
```

**Stall Detection Integration**:
- Evidence blocking contributes to stall detection
- ≥3 critical evidence requests BLOCKED → Case status changes to STALLED
- Stall reason includes evidence unavailability details
- See [Case Lifecycle Management - Stall Detection](./case-lifecycle-management.md#stall-detection) for complete algorithm

**Status Transitions Triggered by Evidence**:
- All critical evidence blocked → IN_PROGRESS to STALLED
- New evidence provided after stall → STALLED to IN_PROGRESS
- Solution verification evidence complete → IN_PROGRESS to RESOLVED

---

## User Interaction Flows

### Evidence Request Flow

```
┌─────────────────────────────────────┐
│  Agent Generates Evidence Requests  │
│  (with acquisition guidance)        │
└─────────────┬───────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Store in CaseDiagnosticState       │
│  evidence_requests (persistent)     │
└─────────────┬───────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Display to User                    │
│  (informational, not clickable)     │
└─────────────┬───────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  User Action (next turn)            │
│  - Provide evidence                 │
│  - Ask question                     │
│  - Report unavailable               │
│  - Ignore                           │
└─────────────┬───────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Classify User Input                │
│  (multi-dimensional)                │
└─────────────┬───────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Update Evidence Tracking           │
│  - Record evidence provided         │
│  - Update request status            │
│  - Update completeness              │
└─────────────┬───────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Agent Responds                     │
│  - Answer user input                │
│  - Analyze new evidence             │
│  - Generate new requests            │
│  - Mark obsolete requests           │
└─────────────────────────────────────┘
```

### File Upload Processing Flow

```
┌─────────────────────────────────────┐
│  User Uploads File (data/ endpoint) │
└─────────────┬───────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Extract and Summarize Content      │
└─────────────┬───────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Classify Against Evidence Requests │
│  (form=DOCUMENT)                    │
└─────────────┬───────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Update Evidence Tracking           │
└─────────────┬───────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Generate Agent Analysis            │
│  - Key findings from file           │
│  - How it advances investigation    │
│  - Next steps                       │
└─────────────┬───────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  IMMEDIATE Feedback to User         │
│  (not waiting for next query)       │
└─────────────────────────────────────┘
```

### Evidence Conflict Resolution Flow

When evidence contradicts the current hypothesis or investigation direction, the system must engage the user to confirm the finding and adjust the investigation path.

```
┌─────────────────────────────────────┐
│  Evidence Classified as REFUTING    │
│  (contradicts hypothesis/direction) │
└─────────────┬───────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Agent Analyzes Contradiction       │
│  - Which hypothesis it refutes      │
│  - Confidence in contradiction      │
│  - Alternative explanations         │
└─────────────┬───────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Communicate to User                │
│  "This evidence contradicts our     │
│   current hypothesis that X.        │
│   Specifically: [detail].           │
│   Can you confirm this finding?"    │
└─────────────┬───────────────────────┘
              │
              ▼
         ┌────┴────┐
         │ User    │
         │ Response│
         └────┬────┘
              │
        ┌─────┴──────┐
        │            │
   Confirms     Disputes
        │            │
        ▼            ▼
┌──────────────┐  ┌──────────────┐
│ Mark Hypo as │  │ Re-classify  │
│ REFUTED      │  │ Evidence as  │
│              │  │ UNCERTAIN    │
└──────┬───────┘  └──────┬───────┘
       │                 │
       ▼                 │
┌──────────────┐         │
│ Generate New │         │
│ Hypotheses   │         │
│ OR           │◄────────┘
│ Revise       │
│ Existing     │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Request New  │
│ Evidence to  │
│ Test Revised │
│ Direction    │
└──────────────┘
```

#### Refuting Evidence Workflow Algorithm

```python
async def handle_refuting_evidence(
    evidence: EvidenceProvided,
    current_hypotheses: List[Hypothesis],
    state: CaseDiagnosticState,
    llm_client: LLMClient
) -> AgentResponse:
    """
    When evidence refutes current hypothesis, confirm with user
    before changing investigation direction.
    """

    # 1. Identify which hypotheses are contradicted
    contradicted = []
    for hypo in current_hypotheses:
        if hypo.status == "active" or hypo.status == "testing":
            contradiction = analyze_contradiction(evidence, hypo, llm_client)
            if contradiction.confidence > 0.7:
                contradicted.append({
                    "hypothesis": hypo,
                    "contradiction": contradiction
                })

    if not contradicted:
        # False alarm, not actually refuting
        return standard_response(evidence)

    # 2. Communicate contradiction to user with confirmation request
    response_content = format_contradiction_message(
        evidence=evidence,
        contradictions=contradicted
    )

    # Example format:
    # "⚠️  Evidence Conflict Detected
    #
    # The evidence you provided contradicts our current hypothesis:
    #
    # **Current Hypothesis**: Database connection pool exhaustion
    #   - We expected: High connection count (>90% pool utilization)
    #   - Your evidence shows: Connection pool at 45% (well within limits)
    #
    # **Evidence Source**: database_metrics.log
    #
    # Can you confirm this finding is accurate? If so, we'll need to
    # revise our hypothesis and explore alternative causes."

    # Mark state as awaiting_confirmation
    state.metadata["awaiting_refutation_confirmation"] = True
    state.metadata["pending_refutations"] = [c["hypothesis"].id for c in contradicted]

    return AgentResponse(
        content=response_content,
        response_type=ResponseType.CLARIFICATION_REQUEST,
        suggested_actions=[
            SuggestedAction(
                label="✅ Confirm - Evidence is accurate",
                query_template="Yes, the evidence is accurate"
            ),
            SuggestedAction(
                label="❌ Dispute - Evidence may be incorrect",
                query_template="Actually, let me recheck that data"
            ),
            SuggestedAction(
                label="🤔 Uncertain - Need to verify",
                query_template="I need to verify this evidence"
            )
        ]
    )

async def process_refutation_confirmation(
    user_confirmation: str,
    state: CaseDiagnosticState
) -> None:
    """Process user's confirmation/dispute of refuting evidence"""

    pending_ids = state.metadata.get("pending_refutations", [])

    if confirms_refutation(user_confirmation):  # "Yes", "Confirm", etc.
        # Mark hypotheses as refuted
        for hypo in state.hypotheses:
            if hypo.id in pending_ids:
                hypo.status = "refuted"
                hypo.refutation_evidence = evidence.evidence_id

        # Trigger hypothesis regeneration
        state.metadata["trigger_hypothesis_regeneration"] = True

    elif disputes_refutation(user_confirmation):  # "No", "Incorrect", etc.
        # Mark evidence as uncertain, don't change hypotheses
        evidence.evidence_type = EvidenceType.NEUTRAL
        evidence.metadata["disputed_refutation"] = True

    else:  # Uncertain
        # Mark for re-verification
        state.evidence_requests.append(EvidenceRequest(
            label="Re-verify contradictory data",
            description="Please re-check the data that showed unexpected results",
            category=EvidenceCategory.SYMPTOMS,
            guidance=AcquisitionGuidance(
                commands=["# Re-run previous command to verify"],
                alternatives=["Cross-check with different data source"]
            ),
            status=EvidenceStatus.PENDING
        ))

    # Clear confirmation state
    state.metadata["awaiting_refutation_confirmation"] = False
    state.metadata.pop("pending_refutations", None)
```

#### Integration with Phase 4 (Validation Agent)

When in Phase 4 (Hypothesis Validation), refuting evidence should trigger special handling:

```python
# In ValidationAgent prompt
"""
When evidence REFUTES your hypothesis:

1. Clearly communicate the contradiction to the user
2. Request confirmation: "Can you confirm this finding is accurate?"
3. Wait for user response before marking hypothesis as refuted
4. If confirmed: Generate alternative hypotheses
5. If disputed: Re-classify evidence as uncertain, continue testing
6. Never silently discard contradictory evidence
"""
```

### Evidence Unavailability Flow

```
User: "I don't have access to the server logs"
   │
   ▼
[Classification: user_intent=REPORTING_UNAVAILABLE, matched_request_ids=[req-001]]
   │
   ▼
[Update req-001: status=BLOCKED, metadata.blocked_reason="no server access"]
   │
   ▼
[Agent Generates Alternative Path]
   │
   ├──> Can someone else provide? (teammate with access)
   ├──> Is there proxy evidence? (monitoring dashboard, APM tool)
   ├──> Can we proceed without it? (narrow scope, reduce confidence)
   └──> Should we escalate? (if critical and no alternatives)
   │
   ▼
Agent Response with Alternatives
```

---

## Safety and Validation

### Command Safety Validation

```python
DANGEROUS_PATTERNS = [
    r'\brm\b.*-rf',              # Recursive delete
    r'\bchmod\b.*777',           # Overly permissive
    r'curl.*\|.*bash',           # Remote code execution
    r'wget.*\|.*sh',             # Remote code execution
    r'/etc/passwd',              # Sensitive file
    r'/etc/shadow',              # Sensitive file
    r'DROP\s+(TABLE|DATABASE)',  # SQL destruction
    r'DELETE\s+FROM.*WHERE',     # SQL deletion (allow only specific safe patterns)
    r'>\s*/dev/sd[a-z]',        # Writing to disk devices
]

def validate_command_safety(command: str) -> Tuple[bool, Optional[str]]:
    """
    Validate command against dangerous patterns.

    Returns:
        (is_safe: bool, reason: Optional[str])
    """
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return False, f"Dangerous pattern detected: {pattern}"

    return True, None


def sanitize_evidence_guidance(guidance: AcquisitionGuidance) -> AcquisitionGuidance:
    """Remove dangerous commands from guidance"""
    safe_commands = []
    for cmd in guidance.commands:
        is_safe, reason = validate_command_safety(cmd)
        if is_safe:
            safe_commands.append(cmd)
        else:
            logger.warning(f"Blocked dangerous command: {cmd}, reason: {reason}")

    guidance.commands = safe_commands
    return guidance
```

### Agent Prompt Safety Rules

```
SAFETY REQUIREMENTS:

1. NEVER suggest commands that:
   ❌ Delete files (rm, shred, unlink)
   ❌ Modify system state (write operations, chmod, chown)
   ❌ Execute remote code (curl|bash, wget|sh)
   ❌ Expose sensitive data (/etc/passwd, /etc/shadow, private keys, API keys)
   ❌ Modify databases (DROP, DELETE, UPDATE without strong WHERE clause)

2. ONLY suggest READ-ONLY commands:
   ✅ View files: cat, tail, head, less, grep, awk
   ✅ Check status: ps, top, systemctl status, docker ps, kubectl get
   ✅ Query APIs: curl -X GET (read-only endpoints)
   ✅ Check logs: journalctl, tail -f

3. For modification needs, describe action, don't provide command:
   WRONG: "Run: sudo systemctl restart app"
   RIGHT: "Ask your system administrator to restart the application service"

4. Add warnings for privileged commands:
   "Run: tail /var/log/syslog (Note: May require sudo access)"
```

---

## API Contracts

### Evidence Request Schema

```json
{
  "request_id": "string (uuid)",
  "label": "string (max 100 chars)",
  "description": "string (max 500 chars)",
  "category": "symptoms|timeline|changes|configuration|scope|metrics|environment",
  "guidance": {
    "commands": ["string", "string", "string"],
    "file_locations": ["string", "string", "string"],
    "ui_locations": ["string", "string", "string"],
    "alternatives": ["string", "string", "string"],
    "prerequisites": ["string", "string"],
    "expected_output": "string (max 200 chars)"
  },
  "status": "pending|partial|complete|blocked|obsolete",
  "created_at_turn": "integer",
  "updated_at_turn": "integer|null",
  "completeness": "float (0.0-1.0)",
  "metadata": {}
}
```

### Agent Response Schema

```json
{
  "schema_version": "3.1.0",
  "content": "string (agent's conversational response)",
  "response_type": "ANSWER|NEEDS_MORE_DATA|...",
  "session_id": "string",
  "view_state": { ... },
  "sources": [ ... ],
  "plan": null,
  "evidence_requests": [
    {
      "request_id": "...",
      "label": "...",
      "description": "...",
      "category": "...",
      "guidance": { ... },
      "status": "...",
      "created_at_turn": 0,
      "completeness": 0.0
    }
  ]
}
```

### Classification Result Schema

```json
{
  "matched_request_ids": ["req-001", "req-002"],
  "completeness": "complete",
  "completeness_score": 0.9,
  "form": "user_input",
  "evidence_type": "supportive",
  "user_intent": "providing_evidence",
  "rationale": "User provided error logs matching request req-001",
  "follow_up_needed": null
}
```

---

## Examples and Reference Implementations

### Example 1: Evidence Request Generation

**Scenario**: BlastRadiusAgent generating evidence requests for impact assessment.

**Input Context**:
```python
{
  "investigation_mode": "ACTIVE_INCIDENT",
  "problem_statement": "API returning 500 errors",
  "current_phase": 1  # Blast Radius
}
```

**Expected Output**:
```json
{
  "evidence_requests": [
    {
      "label": "Error rate metrics - percentage of failed requests",
      "description": "Need to quantify impact: what percentage of requests are failing to determine severity",
      "category": "metrics",
      "guidance": {
        "commands": [
          "grep '500' /var/log/nginx/access.log | wc -l",
          "curl http://monitoring-api/error-rate"
        ],
        "file_locations": [
          "/var/log/nginx/access.log"
        ],
        "ui_locations": [
          "Monitoring Dashboard > API Metrics > Error Rate"
        ],
        "alternatives": [
          "Check APM tool (New Relic, Datadog) for error percentage",
          "Query database: SELECT COUNT(*) FROM requests WHERE status=500"
        ],
        "expected_output": "Number or percentage - if >10% = high severity, >50% = critical"
      }
    },
    {
      "label": "Affected endpoints - which API routes are failing",
      "description": "Determine if all endpoints affected (systemic) or specific routes (isolated)",
      "category": "scope",
      "guidance": {
        "commands": [
          "grep '500' /var/log/app.log | awk '{print $7}' | sort | uniq -c"
        ],
        "file_locations": [
          "/var/log/app.log"
        ],
        "ui_locations": [
          "Monitoring Dashboard > API Routes > Error Breakdown"
        ],
        "alternatives": [
          "Check load balancer logs",
          "Review error tracking tool (Sentry, Rollbar)"
        ],
        "expected_output": "List of endpoints - all routes = systemic issue, specific routes = localized problem"
      }
    }
  ]
}
```

### Example 2: Multi-Dimensional Classification

**Scenario**: User provides partial evidence.

**Input**:
```python
user_input = "I checked the logs, error rate is about 30%, mostly on /api/users endpoint"
active_requests = [
  {request_id: "req-001", label: "Error rate metrics", category: "metrics"},
  {request_id: "req-002", label: "Affected endpoints", category: "scope"}
]
```

**Expected Classification**:
```json
{
  "matched_request_ids": ["req-001", "req-002"],
  "completeness": "over_complete",
  "completeness_score": 1.2,
  "form": "user_input",
  "evidence_type": "supportive",
  "user_intent": "providing_evidence",
  "rationale": "User provided both error rate (30%) and affected endpoint (/api/users), addressing two evidence requests",
  "follow_up_needed": null
}
```

**State Updates**:
```python
# req-001: PENDING → COMPLETE (error rate provided)
# req-002: PENDING → COMPLETE (affected endpoints identified)
```

### Example 3: Active Incident with Escalation

**Scenario**: Critical evidence blocked, trigger escalation.

**State**:
```python
{
  "investigation_mode": "ACTIVE_INCIDENT",
  "case_status": "IN_PROGRESS",
  "evidence_requests": [
    {req_id: "req-001", status: "BLOCKED", blocked_reason: "no production access"},
    {req_id: "req-002", status: "BLOCKED", blocked_reason: "logs rotated"},
    {req_id: "req-003", status: "BLOCKED", blocked_reason: "monitoring down"}
  ]
}
```

**Stall Detection**:
```python
stall_reason = "Multiple critical evidence sources blocked (cannot access logs, configs, metrics)"
case_status = "STALLED"
```

**Agent Response**:
```
"INVESTIGATION STALLED

Cannot proceed - critical evidence blocked:
❌ Production logs: No access
❌ Historical logs: Rotated/deleted
❌ Monitoring data: System down

⚠️ ESCALATION RECOMMENDED

Since this is an active incident (service down), you need someone with:
• Production system access
• Historical log retention
• Monitoring admin access

Who to contact:
• SRE on-call: [on-call rotation link]
• Platform lead: platform-team@company.com
• DevOps escalation: #devops-escalation Slack

Share with them:
• Symptoms: API 500 errors, 30% failure rate on /api/users
• Started: 14:03 (42 minutes ago)
• Impact: All users cannot access user data
• This conversation history

Time is critical - escalate now?"
```

### Example 4: Post-Mortem Confidence Scoring

**Scenario**: Root cause conclusion with confidence.

**State**:
```python
{
  "investigation_mode": "POST_MORTEM",
  "current_phase": 4,
  "evidence_collected": [
    "Error logs: OutOfMemoryError at 14:03",
    "Memory metrics: 98% usage at 14:03",
    "Deployment: New feature deployed 13:55",
    "Missing: Application heap size config"
  ]
}
```

**Root Cause Conclusion**:
```json
{
  "root_cause": "Memory exhaustion due to memory leak in new user caching feature",
  "confidence": 0.75,
  "supporting_evidence": [
    "OutOfMemoryError in logs at 14:03",
    "Memory usage spiked from 60% to 98% at 14:03",
    "New user caching feature deployed 8 minutes before errors (13:55)",
    "Errors stopped after service restart (cleared memory)"
  ],
  "missing_evidence": [
    "Application heap size configuration",
    "Memory profiling during incident",
    "Code review of new caching feature"
  ],
  "alternative_explanations": [
    {
      "hypothesis": "Traffic spike caused normal memory growth",
      "confidence": 0.15,
      "why_less_likely": "Traffic logs show normal load, no unusual spike"
    }
  ],
  "caveats": [
    "Confidence would be 90% with memory profiling data",
    "Assuming heap size wasn't reduced in deployment"
  ]
}
```

**Agent Response**:
```
"ROOT CAUSE ANALYSIS

🎯 Root Cause: Memory leak in new user caching feature
🔢 Confidence: 75% (Medium-High)

📊 Supporting Evidence:
✓ OutOfMemoryError at 14:03
✓ Memory 60% → 98% spike at 14:03
✓ New caching feature deployed 13:55 (8 min before)
✓ Restart resolved issue (cleared leaked memory)

❌ Missing Evidence:
✗ Application heap size configuration
✗ Memory profiling during incident
✗ Code review of caching implementation

📈 Alternative Explanation (15% likely):
Traffic spike caused normal memory growth
→ Less likely: Traffic logs show normal load

⚠️ Caveats:
• Confidence would be 90% with memory profiling
• Assumes heap size unchanged in deployment

🔧 Recommendation:
1. Review new caching feature code for memory leaks
2. Add memory profiling to staging environment
3. Set alerts for memory growth rate
4. Consider memory limit guards in caching logic

Would you like me to generate a case report and runbook?"
```

---

## Document Metadata

**Version**: 2.1  
**Type**: Component Specification (Evidence Layer)  
**Audience**: Engineers implementing evidence collection, prompt engineers, QA  
**Authority**: Authoritative for evidence collection data models and behaviors

**Related Documents**:
- **Parent Framework**: [Investigation Phases and OODA Integration Framework](./investigation-phases-and-ooda-integration.md)
- **Case Management**: [Case Lifecycle Management](./case-lifecycle-management.md)
- **System Architecture**: [Architecture Overview](./architecture-overview.md)

**Document History**:
- v1.0: Initial evidence-centric design (Doctor/Patient Architecture)
- v2.0: Enhanced with multi-dimensional classification, mode-based behavior
- v2.1: Integrated with Investigation Phases Framework, renamed modes to strategies, clarified scope

---

**END OF DESIGN SPECIFICATION**
