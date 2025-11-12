# FaultMaven Evidence Architecture v1.1 (Enhanced)

## Executive Summary

This document specifies FaultMaven's evidence-based troubleshooting data models and workflows. Evidence Architecture is one of three complementary system designs that together implement the complete investigation capability.

**Three-Pillar Architecture**:

```
┌─────────────────────────────────────────────────────────────┐
│ Investigation State and Control Framework                   │
│ (Process Framework)                                         │
│                                                             │
│ • Investigation phases (0-6)                                │
│ • OODA loop integration                                     │
│ • State machine logic                                       │
│ • Phase transitions                                         │
│ • Engagement modes                                          │
│ • Working conclusion tracking                               │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      │ Controls process flow
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│ Prompt Engineering Architecture                             │
│ (LLM Communication)                                         │
│                                                             │
│ • Multi-layer prompt assembly                               │
│ • Phase-specific prompts                                    │
│ • Context management                                        │
│ • Token optimization                                        │
│ • Agent behavior specifications                             │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      │ Shapes LLM behavior
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│ Evidence Architecture v1.1 (THIS DOCUMENT)                  │
│ (Data Collection and Evaluation)                            │
│                                                             │
│ • Evidence data models                                      │
│ • Evidence request structures                               │
│ • Collection workflows                                      │
│ • Classification algorithms                                 │
│ • Hypothesis-evidence linkage                               │
│ • Lifecycle tracking                                        │
└─────────────────────────────────────────────────────────────┘
```

**This Document's Scope**:
- ✅ Evidence and hypothesis data schemas
- ✅ Evidence request and acquisition guidance structures
- ✅ Evidence collection workflow and lifecycle tracking
- ✅ Multi-dimensional classification algorithms
- ✅ Completeness scoring and progress tracking
- ✅ Safety validation for suggested commands
- ✅ Investigation strategy modes (active incident vs post-mortem)

**Out of Scope** (Covered by Other Designs):
- ❌ Investigation phase definitions → See Investigation State and Control Framework
- ❌ OODA loop mechanics → See Investigation State and Control Framework
- ❌ Prompt templates and context assembly → See Prompt Engineering Architecture
- ❌ Phase transition logic → See Investigation State and Control Framework

**Key Capabilities**:
- **Two-layer architecture** - Fact-based core + collection workflow
- **Hypothesis-driven collection** - Evidence requirements defined by hypotheses
- **M:N relationship model** - Evidence can support/refute multiple hypotheses
- **Lifecycle tracking** - Request states (pending/partial/complete/blocked)
- **Dual investigation modes** - Active incident (speed) vs post-mortem (depth)
- **Actionable guidance** - Commands, file paths, UI locations for evidence acquisition
- **Safety by default** - Validation against dangerous command patterns
- **Qualitative reasoning** - Status-based decisions (no brittle numeric confidence)

---

## Table of Contents

1. [Core Principles](#1-core-principles)
2. [Architecture Overview](#2-architecture-overview)
3. [Data Models - Layer 1 (Fact Core)](#3-data-models-layer-1-fact-core)
4. [Data Models - Layer 2 (Collection Workflow)](#4-data-models-layer-2-collection-workflow)
5. [Evidence Collection Workflow](#5-evidence-collection-workflow)
6. [Evidence Evaluation](#6-evidence-evaluation)
7. [Investigation Strategies](#7-investigation-strategies)
8. [Safety and Validation](#8-safety-and-validation)
9. [Integration Points](#9-integration-points)
10. [Implementation Guide](#10-implementation-guide)
11. [Examples](#11-examples)
12. [Appendix](#12-appendix)

---

## 1. Core Principles

### 1.1 Two-Layer Architecture

**Layer 1: Fact Core (What Evidence Exists)**
- Evidence as immutable facts
- Hypotheses as mutable theories
- Relationships tracked qualitatively (reasoning, not numbers)

**Layer 2: Collection Workflow (How to Collect Evidence)**
- Evidence requests with actionable guidance
- Lifecycle tracking (pending → complete/blocked)
- User agency handling (may deviate, provide unsolicited)

### 1.2 Evidence = Processed Information, Not Raw Data

```
Raw File (50KB log) → S3: s3://bucket/case_001/ev_001.log
                    ↓ Preprocessing
Evidence Object → DB: {
    summary: "95/100 connections, 10 idle-in-transaction >5min",
    content_ref: "s3://...",
    hypothesis_support: {
        "hypo_001": "strongly_supports"  // Pool exhaustion
    }
}
```

### 1.3 Evidence Over Questions

**Principle**: Request specific diagnostic data with instructions, not questions.

**Wrong**:
```
"When did this start?"  // User must formulate answer
```

**Right**:
```
Evidence Request: Timeline establishment
Commands: 
  - journalctl --since='1 hour ago' | grep ERROR
  - tail -100 /var/log/app.log
Expected: First error timestamp
```

### 1.4 Hypothesis-Driven Collection

**Principle**: Evidence requirements originate from hypotheses, not generic phase objectives.

```python
# Phase 3: Generate hypothesis WITH evidence requirements
Hypothesis(
    statement="Database pool exhausted due to leak",
    evidence_requirements=[
        EvidenceRequirement(
            description="Connection pool metrics",
            tests_aspect="Pool exhaustion",
            priority="critical",
            acquisition_guidance=AcquisitionGuidance(...)
        )
    ]
)
```

### 1.5 Qualitative Reasoning Over Numeric Scores

**Status-Based Decisions**:
```python
# OLD (brittle)
if hypothesis.confidence >= 0.70: advance_to_solution()

# NEW (robust)
if hypothesis.status == HypothesisStatus.VALIDATED: advance_to_solution()
```

### 1.6 User Agency with Agent Guidance

**Principle**: Users may deviate, ignore requests, or provide unsolicited data. Agent must handle gracefully.

**Implementation**: Multi-dimensional classification handles any input, regardless of whether it matches pending requests.

### 1.7 Safety by Default

**Principle**: Never suggest commands that could cause data loss or security breach.

**Implementation**: Command validation against dangerous patterns before presenting to user.

---

## 2. Architecture Overview

### 2.1 Component Relationships

```
┌──────────────────────────────────────────────────────────┐
│ Case (Investigation Container)                           │
│                                                          │
│ - case_id                                                │
│ - problem_statement                                      │
│ - theories: List[Hypothesis]      ← Theories            │
│ - evidence_log: List[Evidence]    ← Facts               │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│ Layer 1: Fact Core                                       │
│                                                          │
│ Evidence (Immutable Fact)                                │
│ - evidence_id                                            │
│ - summary: "Pool at 95% capacity"                        │
│ - source_type: DATABASE_QUERY                            │
│ - content_ref: S3 URI                                    │
│                                                          │
│ Hypothesis (Mutable Theory)                              │
│ - hypothesis_id                                          │
│ - statement: "Pool exhausted"                            │
│ - status: VALIDATED                                      │
│ - evidence_links: {                                      │
│     "ev_001": EvidenceLink(                              │
│       stance: STRONGLY_SUPPORTS,                         │
│       reasoning: "95% confirms theory",                  │
│       completeness: 0.9                                  │
│     )                                                    │
│   }                                                      │
│ - evidence_requirements: [...]   ← Layer 2 link         │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│ Layer 2: Collection Workflow                             │
│                                                          │
│ EvidenceRequirement (What's Needed)                      │
│ - requirement_id                                         │
│ - description: "Connection pool metrics"                 │
│ - tests_aspect: "Pool exhaustion"                        │
│ - priority: CRITICAL                                     │
│ - acquisition_guidance: {                                │
│     commands: ["curl http://.../metrics"],              │
│     file_locations: ["/var/log/pool.log"],              │
│     ui_locations: ["Dashboard > DB"],                   │
│     expected_output: "Pool usage >90%"                  │
│   }                                                      │
│ - status: PENDING → COMPLETE                             │
│ - fulfilled_by_evidence_ids: ["ev_001"]                 │
└──────────────────────────────────────────────────────────┘
```

### 2.2 Evidence Flow

```
1. Hypothesis Generated (Phase 3)
   ↓
2. Evidence Requirements Defined
   (What evidence needed to test hypothesis)
   ↓
3. Agent Presents Evidence Requests
   (With actionable acquisition guidance)
   ↓
4. User Provides Data
   (Text input or file upload)
   ↓
5. Classification
   (Multi-dimensional: which requests, which hypotheses, completeness, etc.)
   ↓
6. Evidence Object Created
   (Preprocessed summary + S3 reference)
   ↓
7. Hypothesis Linkage
   (Agent analyzes: support/refute/neutral for each hypothesis)
   ↓
8. Status Update
   (Hypothesis status: PROPOSED → TESTING → VALIDATED/REFUTED)
   ↓
9. Lifecycle Tracking
   (Requirement status: PENDING → COMPLETE/BLOCKED)
```

---

## 3. Data Models - Layer 1 (Fact Core)

### 3.1 Evidence Schema

```python
from enum import Enum
from typing import Dict, List, Optional, Any
from datetime import datetime
from pydantic import BaseModel, Field

class EvidenceSourceType(str, Enum):
    """How evidence was obtained"""
    LOG_FILE = "log_file"
    METRICS_DATA = "metrics_data"
    TRACE_DATA = "trace_data"
    DATABASE_QUERY = "database_query"
    COMMAND_OUTPUT = "command_output"
    CODE_REVIEW = "code_review"
    CONFIG_FILE = "config_file"
    SCREENSHOT = "screenshot"
    API_RESPONSE = "api_response"
    USER_OBSERVATION = "user_observation"

class EvidenceForm(str, Enum):
    """Form of evidence provided"""
    USER_INPUT = "user_input"    # Text entered by user
    DOCUMENT = "document"        # File upload (log excerpt, config, screenshot)

class Evidence(BaseModel):
    """
    A processed fact extracted from raw data.
    Immutable. Has no direct knowledge of hypotheses.
    """
    # Identity
    evidence_id: str = Field(description="Unique identifier (UUID)")
    case_id: str = Field(description="Parent investigation case")
    
    # Temporal metadata
    collected_at: datetime = Field(description="When evidence was collected")
    phase_collected: int = Field(ge=0, le=6, description="Investigation phase when collected")
    
    # Classification
    source_type: EvidenceSourceType = Field(
        description="How this evidence was obtained"
    )
    
    form: EvidenceForm = Field(
        description="Text input vs file upload"
    )
    
    # Raw data reference (in S3)
    content_ref: Optional[str] = Field(
        None,
        description="S3 URI to raw artifact: s3://bucket/case_id/evidence_id.ext"
    )
    content_size_bytes: Optional[int] = Field(
        None,
        description="Size of raw artifact in bytes"
    )
    content_type: Optional[str] = Field(
        None,
        description="MIME type of raw artifact"
    )
    
    # Processed data (in DB, for agent)
    summary: str = Field(
        max_length=500,
        description="Concise summary of what this evidence shows (<500 chars)"
    )
    
    # Layer 2 integration: Request linkage
    fulfills_requirement_ids: List[str] = Field(
        default_factory=list,
        description="EvidenceRequirement IDs this evidence satisfies"
    )
    
    # File metadata (if form == DOCUMENT)
    file_metadata: Optional['FileMetadata'] = None
    
    # Provenance
    uploaded_by: Optional[str] = Field(None, description="User who provided evidence")
    uploaded_filename: Optional[str] = Field(None, description="Original filename if upload")
    preprocessed: bool = Field(
        default=True,
        description="Was this processed by preprocessing pipeline?"
    )
    
    # Metadata
    created_at: datetime = Field(default_factory=datetime.utcnow)

class FileMetadata(BaseModel):
    """Metadata for uploaded files"""
    filename: str
    content_type: str = Field(description="MIME type (e.g., text/plain)")
    size_bytes: int
    upload_timestamp: datetime
    file_id: str = Field(description="Reference to stored file")
```

### 3.2 Hypothesis Schema

```python
class HypothesisStatus(str, Enum):
    """Agent's qualitative assessment of hypothesis state"""
    PROPOSED = "proposed"      # Just generated, needs evidence
    TESTING = "testing"        # Collecting evidence, inconclusive
    VALIDATED = "validated"    # Strong evidence, ready for solution
    REFUTED = "refuted"        # Evidence contradicts hypothesis

class EvidenceStance(str, Enum):
    """How evidence relates to hypothesis"""
    STRONGLY_SUPPORTS = "strongly_supports"  # Smoking gun
    SUPPORTS = "supports"                    # Aligns with theory
    NEUTRAL = "neutral"                      # Relevant but doesn't change status
    REFUTES = "refutes"                      # Contradicts theory
    IRRELEVANT = "irrelevant"                # Not related to this hypothesis

class EvidenceLink(BaseModel):
    """
    How a specific piece of evidence relates to this hypothesis.
    This is the VALUE in Hypothesis.evidence_links dictionary.
    """
    stance: EvidenceStance = Field(
        description="How this evidence relates to hypothesis"
    )
    reasoning: str = Field(
        description="Agent's qualitative explanation (LLM's strength)"
    )
    
    # Layer 2 integration: Completeness tracking
    completeness: float = Field(
        ge=0.0, le=1.0,
        description="How well this evidence tests the hypothesis (0.0-1.0)"
    )
    
    # Layer 2 integration: Request tracking
    fulfills_requirement_id: Optional[str] = Field(
        None,
        description="EvidenceRequirement ID this evidence fulfills"
    )
    
    analyzed_at: datetime = Field(default_factory=datetime.utcnow)

class Hypothesis(BaseModel):
    """
    A theory about root cause.
    Tracks relationships to evidence + defines what evidence is needed.
    """
    # Identity
    hypothesis_id: str = Field(description="Unique identifier (UUID)")
    case_id: str = Field(description="Parent investigation case")
    
    # The theory
    statement: str = Field(
        description="Root cause theory (concise, testable)",
        max_length=200
    )
    reasoning: str = Field(
        description="Why this could be the cause"
    )
    
    # Status (replaces numeric confidence)
    status: HypothesisStatus = Field(
        default=HypothesisStatus.PROPOSED,
        description="Agent's qualitative assessment"
    )
    
    # Layer 1: Evidence relationships (M:N via this dictionary)
    evidence_links: Dict[str, EvidenceLink] = Field(
        default_factory=dict,
        description="Maps evidence_id to relationship: {evidence_id: EvidenceLink}"
    )
    
    # Layer 2: Evidence requirements (what's needed to test hypothesis)
    evidence_requirements: List['EvidenceRequirement'] = Field(
        default_factory=list,
        description="Structured requests for evidence to test this hypothesis"
    )
    
    # Metadata
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
```

### 3.3 Case Schema

```python
class CaseStatus(str, Enum):
    """Case lifecycle status"""
    ACTIVE = "active"
    RESOLVED = "resolved"
    CLOSED = "closed"

class Case(BaseModel):
    """
    The complete investigation case file.
    Single source of truth for troubleshooting session.
    """
    # Identity
    case_id: str = Field(description="Unique identifier (UUID)")
    
    # Problem definition
    problem_statement: str = Field(
        description="User's initial problem description"
    )
    status: CaseStatus = Field(
        default=CaseStatus.ACTIVE,
        description="Investigation status"
    )
    
    # Phase 1 data (from Investigation State and Control Framework)
    anomaly_frame: Optional['AnomalyFrame'] = Field(
        None,
        description="Scope and impact assessment (Phase 1)"
    )
    
    # Phase 2 data (from Investigation State and Control Framework)
    timeline: Optional['Timeline'] = Field(
        None,
        description="Temporal context (Phase 2)"
    )
    
    # Phase 3-4 data: The Evidence Board
    theories: List[Hypothesis] = Field(
        default_factory=list,
        description="All hypotheses proposed for this case"
    )
    
    evidence_log: List[Evidence] = Field(
        default_factory=list,
        description="All evidence collected for this case"
    )
    
    # Layer 2: Investigation strategy
    investigation_strategy: Optional['InvestigationStrategy'] = None
    
    # Metadata
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: Optional[str] = None
```

---

## 4. Data Models - Layer 2 (Collection Workflow)

### 4.1 Evidence Requirement Schema

```python
class EvidenceRequirementPriority(str, Enum):
    """Priority of evidence for hypothesis validation"""
    CRITICAL = "critical"      # Hypothesis cannot be validated without this
    IMPORTANT = "important"    # Significantly increases confidence
    OPTIONAL = "optional"      # Nice-to-have confirmation

class EvidenceRequirementStatus(str, Enum):
    """Lifecycle states of evidence requirement"""
    PENDING = "pending"        # Requested, awaiting user
    PARTIAL = "partial"        # Some information provided, need more
    COMPLETE = "complete"      # Fully satisfied
    BLOCKED = "blocked"        # User cannot provide (access/unavailable)
    OBSOLETE = "obsolete"      # No longer relevant to investigation

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

class EvidenceRequirement(BaseModel):
    """
    Structured request for evidence to test hypothesis.
    Part of Hypothesis.evidence_requirements list.
    """
    # Identity
    requirement_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    hypothesis_id: str = Field(description="Parent hypothesis being tested")
    
    # What's needed
    description: str = Field(
        max_length=500,
        description="What evidence is needed and why"
    )
    tests_aspect: str = Field(
        max_length=200,
        description="Which aspect of hypothesis this tests"
    )
    priority: EvidenceRequirementPriority
    
    # How to get it
    acquisition_guidance: AcquisitionGuidance
    
    # Lifecycle tracking
    status: EvidenceRequirementStatus = Field(
        default=EvidenceRequirementStatus.PENDING
    )
    created_at_turn: int = Field(description="Turn when requirement created")
    updated_at_turn: Optional[int] = None
    
    # Completeness tracking
    completeness: float = Field(
        0.0, ge=0.0, le=1.0,
        description="Degree of satisfaction (0.0=none, 1.0=complete)"
    )
    
    # Fulfillment tracking
    fulfilled_by_evidence_ids: List[str] = Field(
        default_factory=list,
        description="Evidence IDs that satisfy this requirement"
    )
    
    # Metadata
    blocked_reason: Optional[str] = Field(
        None,
        description="Why blocked (if status==BLOCKED)"
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)
```

### 4.2 Evidence Classification Schema

```python
class UserIntent(str, Enum):
    """User's intention with input"""
    PROVIDING_EVIDENCE = "providing_evidence"
    ASKING_QUESTION = "asking_question"
    REPORTING_UNAVAILABLE = "reporting_unavailable"
    REPORTING_STATUS = "reporting_status"        # "Working on getting logs"
    CLARIFYING = "clarifying"                    # "What do you mean by..."
    OFF_TOPIC = "off_topic"

class CompletenessLevel(str, Enum):
    """How well evidence answers requirement(s)"""
    PARTIAL = "partial"              # 0.3-0.7: Some info, need more
    COMPLETE = "complete"            # 0.8-1.0: Fully answers requirement
    OVER_COMPLETE = "over_complete"  # Satisfies >1 requirement

class EvidenceClassification(BaseModel):
    """
    Multi-dimensional classification of user input.
    Generated by LLM via function calling.
    """
    
    # Dimension 1: Requirement matching
    matched_requirement_ids: List[str] = Field(
        default_factory=list,
        description="EvidenceRequirement IDs this addresses (can be empty for unsolicited)"
    )
    
    # Dimension 2: Hypothesis matching
    relevant_hypothesis_ids: List[str] = Field(
        default_factory=list,
        description="Hypotheses this evidence is relevant to"
    )
    hypothesis_support: Dict[str, EvidenceStance] = Field(
        default_factory=dict,
        description="Map of hypothesis_id to stance (SUPPORTS/REFUTES/NEUTRAL)"
    )
    
    # Dimension 3: Completeness
    completeness: CompletenessLevel
    completeness_score: float = Field(
        ge=0.0, le=1.0,
        description="Numeric completeness (0.0-1.0)"
    )
    
    # Dimension 4: Form
    form: EvidenceForm
    
    # Dimension 5: Overall type (for backward compatibility)
    evidence_type: EvidenceStance = Field(
        description="Overall evidential value"
    )
    
    # Dimension 6: Intent
    user_intent: UserIntent
    
    # Reasoning
    rationale: str = Field(
        description="Agent's explanation of classification"
    )
    follow_up_needed: Optional[str] = Field(
        None,
        description="What clarification/additional info needed"
    )
```

### 4.3 Investigation Strategy Schema

```python
class InvestigationStrategy(str, Enum):
    """Investigation approach mode"""
    ACTIVE_INCIDENT = "active_incident"    # Service down, speed priority
    POST_MORTEM = "post_mortem"            # Historical analysis, thoroughness priority

class InvestigationMode(BaseModel):
    """
    Investigation mode configuration.
    Influences evidence collection strategy and agent behavior.
    """
    strategy: InvestigationStrategy
    
    # Strategy-specific settings
    prioritize_speed: bool = Field(
        description="True for active incidents (mitigation over complete RCA)"
    )
    require_confidence_score: bool = Field(
        description="True for post-mortems (explicit confidence required)"
    )
    max_evidence_requests_per_turn: int = Field(
        default=3,
        description="How many evidence requests to generate per turn"
    )
    escalation_threshold: int = Field(
        description="Failed attempts before suggesting escalation"
    )
    
    # Timing
    selected_at: datetime = Field(default_factory=datetime.utcnow)
    selection_reason: str = Field(
        description="Why this strategy was chosen"
    )
```

---

## 5. Evidence Collection Workflow

### 5.1 Complete Lifecycle

```
┌─────────────────────────────────────────────────────────────┐
│ Phase 3: Hypothesis Generation                              │
│ Agent generates hypothesis WITH evidence requirements       │
└─────────────┬───────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│ Evidence Requirements Defined                                │
│ - description: "Connection pool metrics"                     │
│ - tests_aspect: "Pool exhaustion"                            │
│ - priority: CRITICAL                                         │
│ - acquisition_guidance: { commands, files, UI, etc. }       │
│ - status: PENDING                                            │
└─────────────┬───────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│ Agent Presents Evidence Requests to User                     │
│ (Rendered from acquisition_guidance)                         │
└─────────────┬───────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│ User Provides Data (Next Turn)                               │
│ - Text input: query/ endpoint                                │
│ - File upload: data/ endpoint                                │
└─────────────┬───────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│ Multi-Dimensional Classification (LLM via Function Calling)  │
│ - Which requirements addressed?                              │
│ - Which hypotheses relevant?                                 │
│ - Completeness level?                                        │
│ - Support/refute/neutral per hypothesis?                     │
│ - User intent?                                               │
└─────────────┬───────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│ Evidence Object Created (if providing evidence)              │
│ - Preprocessing: Extract summary from raw data               │
│ - Storage: Raw artifact → S3, summary → DB                   │
│ - Linkage: fulfills_requirement_ids populated                │
└─────────────┬───────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│ Hypothesis Analysis (Agent via Function Calling)             │
│ For EACH relevant hypothesis:                                │
│ - Update evidence_links[evidence_id] = EvidenceLink(...)    │
│ - stance: STRONGLY_SUPPORTS / SUPPORTS / NEUTRAL / REFUTES  │
│ - reasoning: "Pool at 95% confirms exhaustion theory"       │
│ - completeness: 0.9                                          │
└─────────────┬───────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│ Status Updates                                               │
│ - EvidenceRequirement.status: PENDING → COMPLETE/BLOCKED    │
│ - EvidenceRequirement.completeness: 0.0 → 0.9               │
│ - Hypothesis.status: TESTING → VALIDATED/REFUTED            │
└─────────────┬───────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│ Agent Response to User                                       │
│ - Analysis of new evidence                                   │
│ - Impact on investigation                                    │
│ - Next evidence requests (if needed)                         │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 State Transition Rules

**EvidenceRequirement Status Transitions**:

```
PENDING ──[user provides partial]──> PARTIAL ──[user provides more]──> COMPLETE
   │                                                                    │
   │                                                                    │
   └──[user cannot provide]──> BLOCKED                                │
   │                                                                    │
   └──[investigation moves on]──> OBSOLETE <──────────────────────────┘
```

**Rules**:
1. **PENDING → PARTIAL**: Completeness score 0.3-0.7
2. **PENDING/PARTIAL → COMPLETE**: Completeness score ≥ 0.8
3. **PENDING → BLOCKED**: User reports unavailable + cannot provide
4. **ANY → OBSOLETE**: Hypothesis refuted or investigation direction changed

### 5.3 Completeness Scoring

```python
def calculate_completeness(
    user_input: str,
    requirement: EvidenceRequirement,
    classification: EvidenceClassification,
) -> float:
    """
    Calculate how well user input satisfies requirement.
    
    Scoring guidelines:
    - 1.0: Fully answers requirement, no gaps
    - 0.8-0.9: Answers requirement with minor gaps
    - 0.5-0.7: Partial answer, significant gaps
    - 0.3-0.4: Minimal information, mostly incomplete
    - 0.0-0.2: Barely relevant or empty
    """
    
    # LLM generates score via function calling
    # Based on:
    # - Does input contain expected_output patterns?
    # - Are key data points present?
    # - Are there significant gaps?
    
    return classification.completeness_score

def update_requirement_completeness(
    requirement: EvidenceRequirement,
    new_score: float,
) -> None:
    """
    Update requirement completeness.
    Uses MAX, not accumulation (each evidence independently scored).
    """
    requirement.completeness = max(requirement.completeness, new_score)
    
    # Update status based on completeness
    if requirement.completeness >= 0.8:
        requirement.status = EvidenceRequirementStatus.COMPLETE
    elif requirement.completeness >= 0.3:
        requirement.status = EvidenceRequirementStatus.PARTIAL
```

### 5.4 Evidence Request Generation

**Phase 3 Integration** (from Investigation State and Control Framework):

When agent generates hypotheses in Phase 3, it MUST define evidence requirements:

```python
# In Phase 3 (Hypothesis Generation)
hypothesis = Hypothesis(
    statement="Database connection pool exhausted due to leak",
    reasoning="Timeouts after 2h uptime match pool exhaustion pattern",
    status=HypothesisStatus.PROPOSED,
    evidence_requirements=[
        EvidenceRequirement(
            description="Connection pool utilization metrics showing current vs max",
            tests_aspect="Pool exhaustion",
            priority=EvidenceRequirementPriority.CRITICAL,
            acquisition_guidance=AcquisitionGuidance(
                commands=[
                    "curl http://localhost:8080/metrics | grep pool",
                    "psql -c 'SELECT count(*) FROM pg_stat_activity'"
                ],
                file_locations=["/var/log/db-pool.log"],
                ui_locations=["Dashboard > Database > Pool Metrics"],
                alternatives=["Check APM tool connection graphs"],
                expected_output="Pool usage >90% indicates exhaustion"
            ),
            created_at_turn=current_turn,
        ),
        EvidenceRequirement(
            description="Connection lifecycle in application code",
            tests_aspect="Connection leak detection",
            priority=EvidenceRequirementPriority.CRITICAL,
            acquisition_guidance=AcquisitionGuidance(
                commands=[],  # Code review, not command
                file_locations=[
                    "src/db/connection_manager.py",
                    "src/services/data_access.py"
                ],
                ui_locations=["GitHub > Repository > DB Code"],
                alternatives=["Check IDE for connection.close() calls"],
                expected_output="Missing conn.close() in error handlers"
            ),
            created_at_turn=current_turn,
        )
    ]
)
```

---

## 6. Evidence Evaluation

### 6.1 Classification Algorithm

```python
async def classify_evidence(
    user_input: str,
    active_requirements: List[EvidenceRequirement],
    active_hypotheses: List[Hypothesis],
    conversation_history: List[Message],
    llm_client: LLMClient
) -> EvidenceClassification:
    """
    Classify user input across 6 dimensions using LLM function calling.
    
    See Prompt Engineering Architecture for complete prompt template.
    """
    
    # Build context
    context = {
        "user_input": user_input,
        "active_requirements": format_requirements(active_requirements),
        "active_hypotheses": format_hypotheses(active_hypotheses),
    }
    
    # Call LLM with classification tool
    response = await llm_client.generate_with_tools(
        messages=[{"role": "user", "content": build_classification_prompt(context)}],
        tools=[classify_evidence_tool],
        model="claude-sonnet-4-5-20250929",
    )
    
    # Extract classification from tool call
    if response.tool_calls:
        tool_input = response.tool_calls[0].input
        return EvidenceClassification(**tool_input)
    
    # Fallback: treat as question
    return EvidenceClassification(
        matched_requirement_ids=[],
        relevant_hypothesis_ids=[],
        hypothesis_support={},
        completeness=CompletenessLevel.PARTIAL,
        completeness_score=0.0,
        form=EvidenceForm.USER_INPUT,
        evidence_type=EvidenceStance.NEUTRAL,
        user_intent=UserIntent.ASKING_QUESTION,
        rationale="Could not classify, treating as question",
    )
```

### 6.2 Classification Tool Definition

```python
classify_evidence_tool = {
    "name": "classify_evidence",
    "description": "Classify user input across multiple dimensions",
    "input_schema": {
        "type": "object",
        "properties": {
            "matched_requirement_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "EvidenceRequirement IDs this addresses (can be empty)"
            },
            "relevant_hypothesis_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Hypotheses this evidence is relevant to"
            },
            "hypothesis_support": {
                "type": "object",
                "description": "Map of hypothesis_id to stance",
                "additionalProperties": {
                    "type": "string",
                    "enum": ["strongly_supports", "supports", "neutral", "refutes", "irrelevant"]
                }
            },
            "completeness": {
                "type": "string",
                "enum": ["partial", "complete", "over_complete"]
            },
            "completeness_score": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0
            },
            "form": {
                "type": "string",
                "enum": ["user_input", "document"]
            },
            "evidence_type": {
                "type": "string",
                "enum": ["strongly_supports", "supports", "neutral", "refutes", "irrelevant"]
            },
            "user_intent": {
                "type": "string",
                "enum": [
                    "providing_evidence",
                    "asking_question",
                    "reporting_unavailable",
                    "reporting_status",
                    "clarifying",
                    "off_topic"
                ]
            },
            "rationale": {"type": "string"},
            "follow_up_needed": {"type": "string"}
        },
        "required": [
            "matched_requirement_ids",
            "relevant_hypothesis_ids",
            "hypothesis_support",
            "completeness",
            "completeness_score",
            "form",
            "evidence_type",
            "user_intent",
            "rationale"
        ]
    }
}
```

### 6.3 Hypothesis Analysis

After classification, agent analyzes evidence impact on each hypothesis:

```python
async def analyze_evidence_impact(
    evidence: Evidence,
    classification: EvidenceClassification,
    case: Case,
) -> Case:
    """
    Update hypothesis evidence_links based on classification.
    Uses function calling for structured updates.
    """
    
    for hypothesis_id, stance in classification.hypothesis_support.items():
        hypothesis = next(h for h in case.theories if h.hypothesis_id == hypothesis_id)
        
        # Determine which requirement this fulfills (if any)
        fulfills_req_id = None
        for req in hypothesis.evidence_requirements:
            if req.requirement_id in classification.matched_requirement_ids:
                fulfills_req_id = req.requirement_id
                break
        
        # Calculate completeness for this hypothesis
        # (Different from requirement completeness - how well it tests hypothesis)
        completeness = calculate_hypothesis_test_completeness(
            evidence=evidence,
            hypothesis=hypothesis,
            classification=classification,
        )
        
        # Create evidence link
        hypothesis.evidence_links[evidence.evidence_id] = EvidenceLink(
            stance=EvidenceStance(stance),
            reasoning=f"Evidence classified as {stance}. {classification.rationale}",
            completeness=completeness,
            fulfills_requirement_id=fulfills_req_id,
            analyzed_at=datetime.utcnow(),
        )
        
        # Update requirement status if linked
        if fulfills_req_id:
            req = next(r for r in hypothesis.evidence_requirements if r.requirement_id == fulfills_req_id)
            req.fulfilled_by_evidence_ids.append(evidence.evidence_id)
            req.completeness = max(req.completeness, classification.completeness_score)
            
            if req.completeness >= 0.8:
                req.status = EvidenceRequirementStatus.COMPLETE
            elif req.completeness >= 0.3:
                req.status = EvidenceRequirementStatus.PARTIAL
        
        # Evaluate if hypothesis status should change
        new_status = await evaluate_hypothesis_status(hypothesis, case)
        if new_status != hypothesis.status:
            hypothesis.status = new_status
            hypothesis.updated_at = datetime.utcnow()
    
    return case
```

---

## 7. Investigation Strategies

### 7.1 Strategy Selection

```python
def select_investigation_strategy(
    urgency_level: UrgencyLevel,
    problem_statement: str,
    has_active_problem: bool,
) -> InvestigationMode:
    """
    Select investigation strategy based on context.
    Called after Phase 0 (Intake) when Lead Investigator Mode activated.
    
    See Investigation State and Control Framework for engagement mode system.
    """
    
    # Check for post-mortem indicators
    problem = problem_statement.lower() if problem_statement else ""
    post_mortem_indicators = [
        "what caused", "root cause of", "why did",
        "happened yesterday", "last week", "after the incident",
        "retrospective", "post-mortem", "rca"
    ]
    
    if any(indicator in problem for indicator in post_mortem_indicators):
        return InvestigationMode(
            strategy=InvestigationStrategy.POST_MORTEM,
            prioritize_speed=False,
            require_confidence_score=True,
            max_evidence_requests_per_turn=3,
            escalation_threshold=5,  # More attempts before escalating
            selection_reason="Post-mortem indicators detected in problem statement",
        )
    
    # Check for active incident
    if (urgency_level in [UrgencyLevel.HIGH, UrgencyLevel.CRITICAL]
        and has_active_problem):
        return InvestigationMode(
            strategy=InvestigationStrategy.ACTIVE_INCIDENT,
            prioritize_speed=True,
            require_confidence_score=False,
            max_evidence_requests_per_turn=2,  # Focus on critical evidence only
            escalation_threshold=3,  # Escalate quickly
            selection_reason="Active high-urgency incident detected",
        )
    
    # Default to post-mortem (safer, more thorough)
    return InvestigationMode(
        strategy=InvestigationStrategy.POST_MORTEM,
        prioritize_speed=False,
        require_confidence_score=True,
        max_evidence_requests_per_turn=3,
        escalation_threshold=5,
        selection_reason="Default to thorough investigation",
    )
```

### 7.2 Strategy-Specific Behavior

| Aspect | Active Incident | Post-Mortem |
|--------|----------------|-------------|
| **Objective** | Restore service quickly | Understand root cause completely |
| **Evidence Priority** | Critical only (symptoms, recent changes) | Comprehensive (all categories) |
| **Hypothesis Testing** | Test mitigation immediately | Validate with multiple data points |
| **Phase Advancement** | Can skip phases for speed | Complete all phases |
| **Escalation** | After 3 failed attempts or critical evidence blocked | After hypothesis space exhausted |
| **Confidence** | Not required (mitigation over certainty) | Required with explicit qualitative status |
| **Time Pressure** | Minutes/hours matter | Days acceptable |
| **Acceptable Outcomes** | Service restored (even without root cause) | Definitive root cause or "cannot determine" with caveats |

### 7.3 Strategy Integration with Prompts

**Active Incident Strategy** (injected into Layer 3 of Prompt Engineering Architecture):

```
INVESTIGATION STRATEGY: ACTIVE_INCIDENT

URGENCY-DRIVEN APPROACH:
1. GOAL: Mitigation first, root cause later
2. EVIDENCE: Prioritize symptoms and recent changes only
3. HYPOTHESIS: Form quick hypothesis, test mitigation immediately
4. VALIDATION: Confirm service restored (full validation later)

EVIDENCE REQUEST FOCUS:
- Critical priority evidence only
- Max 2 requests per turn (reduce cognitive load)
- Actionable mitigations over deep analysis

ESCALATION TRIGGER:
- 3 failed mitigation attempts
- Critical evidence blocked (no logs access, no deploy history)
- Symptoms worsening despite actions
```

**Post-Mortem Strategy** (injected into Layer 3):

```
INVESTIGATION STRATEGY: POST_MORTEM

DEPTH-DRIVEN APPROACH:
1. GOAL: Complete root cause understanding
2. EVIDENCE: Comprehensive collection across all categories
3. HYPOTHESIS: Multiple hypotheses, test systematically
4. VALIDATION: Require strong evidence for conclusions

EVIDENCE REQUEST FOCUS:
- All priority levels (critical, important, optional)
- Max 3 requests per turn
- Follow all leads thoroughly

CONFIDENCE REQUIREMENT:
When concluding root cause, hypothesis must reach VALIDATED status with:
- Supporting evidence from multiple categories
- Refutation of alternative explanations
- Explicit caveats about missing evidence
```

---

## 8. Safety and Validation

### 8.1 Command Safety Validation

```python
import re
from typing import Tuple, Optional

DANGEROUS_PATTERNS = [
    r'\brm\b.*-rf',              # Recursive delete
    r'\bchmod\b.*777',           # Overly permissive
    r'curl.*\|.*bash',           # Remote code execution
    r'wget.*\|.*sh',             # Remote code execution
    r'/etc/passwd',              # Sensitive file
    r'/etc/shadow',              # Sensitive file
    r'DROP\s+(TABLE|DATABASE)',  # SQL destruction
    r'DELETE\s+FROM',            # SQL deletion (unsafe without WHERE)
    r'UPDATE\s+.*SET',           # SQL update (unsafe without WHERE)
    r'>\s*/dev/sd[a-z]',        # Writing to disk devices
    r'mkfs',                     # Format filesystem
    r'dd\s+.*of=',              # Direct disk write
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

def sanitize_acquisition_guidance(guidance: AcquisitionGuidance) -> AcquisitionGuidance:
    """
    Remove dangerous commands from guidance before presenting to user.
    """
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

### 8.2 Agent Prompt Safety Rules

**Injected into Prompt Engineering Architecture** (Layer 2 or 3):

```
COMMAND SAFETY REQUIREMENTS:

1. NEVER suggest commands that:
   ❌ Delete files (rm, shred, unlink)
   ❌ Modify system state (write operations, chmod, chown)
   ❌ Execute remote code (curl|bash, wget|sh)
   ❌ Expose sensitive data (/etc/passwd, /etc/shadow, keys)
   ❌ Modify databases (DROP, DELETE, UPDATE without strong WHERE)

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

### 8.3 Input Validation

```python
def validate_evidence_requirement(req: EvidenceRequirement) -> List[str]:
    """
    Validate evidence requirement before storing.
    Returns list of validation errors (empty if valid).
    """
    errors = []
    
    # Validate guidance constraints
    if len(req.acquisition_guidance.commands) > 3:
        errors.append("Too many commands (max 3)")
    
    if len(req.acquisition_guidance.file_locations) > 3:
        errors.append("Too many file locations (max 3)")
    
    if len(req.acquisition_guidance.ui_locations) > 3:
        errors.append("Too many UI locations (max 3)")
    
    if len(req.acquisition_guidance.alternatives) > 3:
        errors.append("Too many alternatives (max 3)")
    
    if len(req.acquisition_guidance.prerequisites) > 2:
        errors.append("Too many prerequisites (max 2)")
    
    # Validate command safety
    for cmd in req.acquisition_guidance.commands:
        is_safe, reason = validate_command_safety(cmd)
        if not is_safe:
            errors.append(f"Unsafe command: {cmd} ({reason})")
    
    # Validate length constraints
    if len(req.description) > 500:
        errors.append("Description too long (max 500 chars)")
    
    if len(req.tests_aspect) > 200:
        errors.append("tests_aspect too long (max 200 chars)")
    
    if req.acquisition_guidance.expected_output and len(req.acquisition_guidance.expected_output) > 200:
        errors.append("expected_output too long (max 200 chars)")
    
    return errors
```

---

## 9. Integration Points

### 9.1 Integration with Investigation State and Control Framework

**Process Flow Integration**:

```python
# Phase 3: Hypothesis Generation (from State and Control Framework)
# → Evidence Architecture: Hypothesis.evidence_requirements created

# Phase 4: Validation (from State and Control Framework)
# → Evidence Architecture: Evidence collected, classified, linked to hypotheses

# Working Conclusion (from State and Control Framework)
# → Evidence Architecture: Based on Hypothesis.status and evidence_links
```

**Key Integration Points**:

1. **Phase Transitions**: Evidence completeness influences phase advancement
   ```python
   # In ValidationAgent (from State and Control Framework)
   def should_advance_to_phase_5(case: Case) -> bool:
       # Check if any hypothesis reached VALIDATED status
       return any(h.status == HypothesisStatus.VALIDATED for h in case.theories)
   ```

2. **Working Conclusion**: Evidence informs confidence assessment
   ```python
   # Working conclusion references evidence
   working_conclusion = WorkingConclusion(
       statement=validated_hypothesis.statement,
       confidence_level="confident",  # Based on status
       supporting_evidence=[
           link.reasoning 
           for link in validated_hypothesis.evidence_links.values()
           if link.stance in [EvidenceStance.STRONGLY_SUPPORTS, EvidenceStance.SUPPORTS]
       ],
       caveats=[
           f"Missing: {req.description}"
           for req in validated_hypothesis.evidence_requirements
           if req.status == EvidenceRequirementStatus.BLOCKED
       ]
   )
   ```

3. **Progress Tracking**: Evidence completeness tracked
   ```python
   # Progress metrics reference evidence requirements
   progress_metrics = ProgressMetrics(
       evidence_completeness=calculate_evidence_completeness(case),
       evidence_complete_count=count_complete_requirements(case),
       evidence_pending_count=count_pending_requirements(case),
       evidence_blocked_count=count_blocked_requirements(case),
   )
   ```

### 9.2 Integration with Prompt Engineering Architecture

**Prompt Layer Integration**:

**Layer 4: Phase-Specific Context** (from Prompt Engineering Architecture)
- Evidence requirements rendered as part of phase context
- Active evidence requests included in prompt

**Layer 5: Investigation State Context** (from Prompt Engineering Architecture)
- Evidence summary (top 5 recent)
- Evidence completeness percentage
- Blocked evidence count

**Example Integration**:

```python
# From Prompt Engineering Architecture Layer 5
def get_investigation_context(case: Case) -> str:
    # Evidence summary
    recent_evidence = case.evidence_log[-5:]
    evidence_summary = "\n".join([
        f"[{ev.source_type.value}] {ev.summary}"
        for ev in recent_evidence
    ])
    
    # Evidence completeness (from Evidence Architecture)
    total_requirements = sum(len(h.evidence_requirements) for h in case.theories)
    complete_requirements = sum(
        1 for h in case.theories
        for req in h.evidence_requirements
        if req.status == EvidenceRequirementStatus.COMPLETE
    )
    completeness_pct = (complete_requirements / total_requirements * 100) if total_requirements > 0 else 0
    
    return f"""
# Investigation Status

## Recent Evidence ({len(recent_evidence)} of {len(case.evidence_log)} total)
{evidence_summary}

## Evidence Completeness
- Total requirements: {total_requirements}
- Complete: {complete_requirements} ({completeness_pct:.0f}%)
- Pending: {count_pending(case)}
- Blocked: {count_blocked(case)}
"""
```

### 9.3 Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│ Investigation State and Control Framework                   │
│                                                             │
│ Phase 3: Hypothesis Generation                              │
│ → Creates Hypothesis with evidence_requirements             │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│ Evidence Architecture                                        │
│                                                             │
│ EvidenceRequirement objects stored in Hypothesis            │
│ → acquisition_guidance used for requests                    │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│ Prompt Engineering Architecture                             │
│                                                             │
│ Layer 4: Phase context includes evidence requests           │
│ → Rendered to user with commands, files, UI paths           │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼ User provides data
┌─────────────────────────────────────────────────────────────┐
│ Evidence Architecture                                        │
│                                                             │
│ Classification (LLM via function calling)                   │
│ → EvidenceClassification with 6 dimensions                  │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│ Evidence Architecture                                        │
│                                                             │
│ Evidence object created                                     │
│ → Preprocessing: summary extracted                          │
│ → Storage: Raw → S3, Summary → DB                           │
│ → Linkage: fulfills_requirement_ids populated               │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│ Evidence Architecture                                        │
│                                                             │
│ Hypothesis analysis (LLM via function calling)              │
│ → Update Hypothesis.evidence_links                          │
│ → Update EvidenceRequirement.status                         │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│ Investigation State and Control Framework                   │
│                                                             │
│ Status evaluation (qualitative)                             │
│ → Hypothesis.status: TESTING → VALIDATED/REFUTED           │
│ → Phase advancement check                                   │
└─────────────────────────────────────────────────────────────┘
```

---

## 10. Implementation Guide

### 10.1 Service Architecture

```python
# services/evidence_service.py
class EvidenceService:
    """Manages evidence lifecycle"""
    
    async def create_evidence(
        self,
        preprocessing_result: PreprocessingResult,
        case_id: str,
        phase: int,
        classification: EvidenceClassification,
        uploaded_by: str,
    ) -> Evidence:
        """Create evidence from preprocessing result and classification"""
        
        evidence = Evidence(
            evidence_id=generate_uuid(),
            case_id=case_id,
            collected_at=datetime.utcnow(),
            phase_collected=phase,
            source_type=infer_source_type(preprocessing_result),
            form=classification.form,
            content_ref=preprocessing_result.content_ref,
            content_size_bytes=preprocessing_result.content_size_bytes,
            content_type=preprocessing_result.content_type,
            summary=preprocessing_result.summary,
            fulfills_requirement_ids=classification.matched_requirement_ids,
            file_metadata=create_file_metadata(preprocessing_result) if classification.form == EvidenceForm.DOCUMENT else None,
            uploaded_by=uploaded_by,
            preprocessed=True,
        )
        
        await self.repository.save(evidence)
        return evidence


# services/hypothesis_service.py
class HypothesisService:
    """Manages hypothesis lifecycle including evidence requirements"""
    
    async def create_hypothesis_with_requirements(
        self,
        case_id: str,
        statement: str,
        reasoning: str,
        evidence_requirements: List[Dict[str, Any]],
        current_turn: int,
    ) -> Hypothesis:
        """Create hypothesis with evidence requirements"""
        
        hypothesis = Hypothesis(
            hypothesis_id=generate_uuid(),
            case_id=case_id,
            statement=statement,
            reasoning=reasoning,
            status=HypothesisStatus.PROPOSED,
        )
        
        # Create evidence requirements
        for req_data in evidence_requirements:
            req = EvidenceRequirement(
                hypothesis_id=hypothesis.hypothesis_id,
                description=req_data["description"],
                tests_aspect=req_data["tests_aspect"],
                priority=EvidenceRequirementPriority(req_data["priority"]),
                acquisition_guidance=AcquisitionGuidance(**req_data["guidance"]),
                created_at_turn=current_turn,
            )
            
            # Validate safety
            errors = validate_evidence_requirement(req)
            if errors:
                logger.warning(f"Evidence requirement validation errors: {errors}")
                req.acquisition_guidance = sanitize_acquisition_guidance(req.acquisition_guidance)
            
            hypothesis.evidence_requirements.append(req)
        
        await self.repository.save(hypothesis)
        return hypothesis
    
    async def update_evidence_link(
        self,
        hypothesis_id: str,
        evidence_id: str,
        stance: EvidenceStance,
        reasoning: str,
        completeness: float,
        fulfills_requirement_id: Optional[str],
    ) -> Hypothesis:
        """Add or update evidence link"""
        
        hypothesis = await self.repository.get(hypothesis_id)
        
        hypothesis.evidence_links[evidence_id] = EvidenceLink(
            stance=stance,
            reasoning=reasoning,
            completeness=completeness,
            fulfills_requirement_id=fulfills_requirement_id,
            analyzed_at=datetime.utcnow(),
        )
        hypothesis.updated_at = datetime.utcnow()
        
        # Update requirement if linked
        if fulfills_requirement_id:
            req = next(r for r in hypothesis.evidence_requirements if r.requirement_id == fulfills_requirement_id)
            req.fulfilled_by_evidence_ids.append(evidence_id)
            # Completeness updated separately by classification
        
        await self.repository.save(hypothesis)
        return hypothesis


# services/classification_service.py
class ClassificationService:
    """Handles evidence classification"""
    
    async def classify_user_input(
        self,
        user_input: str,
        case: Case,
    ) -> EvidenceClassification:
        """Classify user input across 6 dimensions"""
        
        # Gather active requirements
        active_requirements = [
            req for hypothesis in case.theories
            for req in hypothesis.evidence_requirements
            if req.status in [
                EvidenceRequirementStatus.PENDING,
                EvidenceRequirementStatus.PARTIAL
            ]
        ]
        
        # Gather active hypotheses
        active_hypotheses = [
            h for h in case.theories
            if h.status in [HypothesisStatus.PROPOSED, HypothesisStatus.TESTING]
        ]
        
        # Call LLM via function calling
        classification = await classify_evidence(
            user_input=user_input,
            active_requirements=active_requirements,
            active_hypotheses=active_hypotheses,
            conversation_history=[],  # From context
            llm_client=self.llm_client,
        )
        
        return classification
```

### 10.2 API Endpoints

```python
# api/evidence.py
from fastapi import APIRouter, UploadFile, Depends

router = APIRouter()

@router.post("/cases/{case_id}/evidence")
async def upload_evidence(
    case_id: str,
    file: UploadFile,
    current_user: User = Depends(get_current_user),
):
    """
    Upload evidence file for case.
    Triggers: preprocessing → classification → evidence creation → hypothesis analysis
    """
    
    # 1. Load case
    case = await case_service.get_case_with_relations(case_id)
    
    # 2. Preprocessing pipeline
    preprocessing_result = await preprocessing_service.process_upload(
        file=file,
        case_id=case_id,
    )
    
    # 3. Classification
    classification = await classification_service.classify_user_input(
        user_input=preprocessing_result.summary,
        case=case,
    )
    
    # 4. Create evidence
    evidence = await evidence_service.create_evidence(
        preprocessing_result=preprocessing_result,
        case_id=case_id,
        phase=case.current_phase,
        classification=classification,
        uploaded_by=current_user.email,
    )
    
    # 5. Hypothesis analysis
    case = await hypothesis_analysis_service.analyze_evidence_impact(
        evidence=evidence,
        classification=classification,
        case=case,
    )
    
    # 6. Update requirement statuses
    case = await update_requirement_lifecycle(
        classification=classification,
        case=case,
    )
    
    # 7. Save case
    await case_service.save(case)
    
    # 8. Generate agent response
    agent_response = await agent_service.generate_evidence_analysis_response(
        evidence=evidence,
        case=case,
    )
    
    return {
        "evidence_id": evidence.evidence_id,
        "summary": evidence.summary,
        "agent_response": agent_response.content,
    }


@router.get("/cases/{case_id}/evidence-requirements")
async def get_evidence_requirements(case_id: str):
    """Get active evidence requirements for case"""
    
    case = await case_service.get_case_with_relations(case_id)
    
    active_requirements = [
        {
            "requirement_id": req.requirement_id,
            "hypothesis_id": req.hypothesis_id,
            "hypothesis_statement": next(
                h.statement for h in case.theories 
                if h.hypothesis_id == req.hypothesis_id
            ),
            "description": req.description,
            "priority": req.priority.value,
            "status": req.status.value,
            "completeness": req.completeness,
            "acquisition_guidance": {
                "commands": req.acquisition_guidance.commands,
                "file_locations": req.acquisition_guidance.file_locations,
                "ui_locations": req.acquisition_guidance.ui_locations,
                "alternatives": req.acquisition_guidance.alternatives,
                "expected_output": req.acquisition_guidance.expected_output,
            }
        }
        for hypothesis in case.theories
        for req in hypothesis.evidence_requirements
        if req.status in [
            EvidenceRequirementStatus.PENDING,
            EvidenceRequirementStatus.PARTIAL
        ]
    ]
    
    return {"requirements": active_requirements}
```

### 10.3 Configuration

```bash
# .env - Evidence Architecture Configuration

# Evidence storage
S3_BUCKET_EVIDENCE=faultmaven-evidence
EVIDENCE_SUMMARY_MAX_LENGTH=500

# Classification
CLASSIFICATION_MODEL=claude-sonnet-4-5-20250929
CLASSIFICATION_TEMPERATURE=0.0

# Evidence requirements
MAX_COMMANDS_PER_GUIDANCE=3
MAX_FILE_LOCATIONS_PER_GUIDANCE=3
MAX_UI_LOCATIONS_PER_GUIDANCE=3
MAX_ALTERNATIVES_PER_GUIDANCE=3
MAX_PREREQUISITES_PER_GUIDANCE=2

# Safety
COMMAND_SAFETY_VALIDATION_ENABLED=true
BLOCK_DANGEROUS_COMMANDS=true

# Investigation strategy
DEFAULT_STRATEGY=post_mortem
ACTIVE_INCIDENT_ESCALATION_THRESHOLD=3
POST_MORTEM_ESCALATION_THRESHOLD=5
```

---

## 11. Examples

### 11.1 Complete Flow Example

```python
# ===================================================================
# Phase 3: Hypothesis Generation with Evidence Requirements
# ===================================================================

hypothesis_1 = Hypothesis(
    hypothesis_id="hypo_001",
    case_id="case_123",
    statement="Database connection pool exhausted due to connection leak",
    reasoning="Symptoms (timeouts after 2h uptime) match pool exhaustion pattern",
    status=HypothesisStatus.PROPOSED,
    evidence_requirements=[
        EvidenceRequirement(
            requirement_id="req_001",
            hypothesis_id="hypo_001",
            description="Connection pool utilization metrics showing current vs max connections",
            tests_aspect="Pool exhaustion",
            priority=EvidenceRequirementPriority.CRITICAL,
            acquisition_guidance=AcquisitionGuidance(
                commands=[
                    "curl http://localhost:8080/metrics | grep pool",
                    "psql -c 'SELECT count(*) FROM pg_stat_activity'"
                ],
                file_locations=["/var/log/db-pool.log"],
                ui_locations=["Dashboard > Database > Pool Metrics"],
                alternatives=["Check APM tool connection graphs"],
                expected_output="Pool usage >90% indicates exhaustion"
            ),
            status=EvidenceRequirementStatus.PENDING,
            created_at_turn=8,
        )
    ]
)

case.theories.append(hypothesis_1)

# ===================================================================
# User Provides Evidence
# ===================================================================

# User runs: psql -c 'SELECT count(*) FROM pg_stat_activity'
# Result: 95

# Preprocessing extracts summary
preprocessing_result = PreprocessingResult(
    summary="Database query shows 95 active connections",
    content_ref="s3://bucket/case_123/ev_001.txt",
    content_size_bytes=256,
    content_type="text/plain",
)

# ===================================================================
# Classification (LLM via Function Calling)
# ===================================================================

classification = EvidenceClassification(
    matched_requirement_ids=["req_001"],
    relevant_hypothesis_ids=["hypo_001"],
    hypothesis_support={
        "hypo_001": "strongly_supports"  # Pool at 95% confirms theory
    },
    completeness=CompletenessLevel.COMPLETE,
    completeness_score=0.9,
    form=EvidenceForm.USER_INPUT,
    evidence_type=EvidenceStance.STRONGLY_SUPPORTS,
    user_intent=UserIntent.PROVIDING_EVIDENCE,
    rationale="User provided connection count (95) which is very high, strongly supports pool exhaustion hypothesis",
    follow_up_needed=None,
)

# ===================================================================
# Evidence Object Created
# ===================================================================

evidence_1 = Evidence(
    evidence_id="ev_001",
    case_id="case_123",
    collected_at=datetime.utcnow(),
    phase_collected=4,
    source_type=EvidenceSourceType.DATABASE_QUERY,
    form=EvidenceForm.USER_INPUT,
    content_ref="s3://bucket/case_123/ev_001.txt",
    content_size_bytes=256,
    summary="Database: 95 active connections",
    fulfills_requirement_ids=["req_001"],
    preprocessed=True,
)

case.evidence_log.append(evidence_1)

# ===================================================================
# Hypothesis Analysis (Agent via Function Calling)
# ===================================================================

# Agent updates hypothesis evidence_links
hypothesis_1.evidence_links["ev_001"] = EvidenceLink(
    stance=EvidenceStance.STRONGLY_SUPPORTS,
    reasoning="95 active connections indicates near-capacity pool utilization, "
              "strongly confirms pool exhaustion hypothesis",
    completeness=0.9,
    fulfills_requirement_id="req_001",
    analyzed_at=datetime.utcnow(),
)

# ===================================================================
# Lifecycle Updates
# ===================================================================

# Update requirement
req_001 = hypothesis_1.evidence_requirements[0]
req_001.status = EvidenceRequirementStatus.COMPLETE
req_001.completeness = 0.9
req_001.fulfilled_by_evidence_ids = ["ev_001"]
req_001.updated_at_turn = 9

# Status evaluation (qualitative)
# Agent determines: Have strong supporting evidence → VALIDATED
hypothesis_1.status = HypothesisStatus.VALIDATED
hypothesis_1.updated_at = datetime.utcnow()

# ===================================================================
# Phase Advancement Check
# ===================================================================

if should_advance_to_phase_5(case):  # True (hypo_001 is VALIDATED)
    print("✅ ROOT CAUSE VALIDATED - Advancing to Phase 5 (Solution)")
```

### 11.2 Unsolicited Evidence Example

```python
# User volunteers evidence before requested

# Agent has hypothesis but hasn't yet requested specific evidence
hypothesis = Hypothesis(
    hypothesis_id="hypo_001",
    statement="Memory leak in auth service",
    status=HypothesisStatus.PROPOSED,
    evidence_requirements=[
        # Requirements exist but status=PENDING (not yet presented to user)
    ]
)

# User proactively uploads GC logs
User: "I found GC logs showing issues, here they are"
User: [uploads gc.log]

# ===================================================================
# Classification
# ===================================================================

classification = EvidenceClassification(
    matched_requirement_ids=[],  # Empty - wasn't requested yet
    relevant_hypothesis_ids=["hypo_001"],
    hypothesis_support={
        "hypo_001": "strongly_supports"
    },
    completeness=CompletenessLevel.COMPLETE,
    completeness_score=1.0,  # Complete even though unsolicited
    form=EvidenceForm.DOCUMENT,
    evidence_type=EvidenceStance.STRONGLY_SUPPORTS,
    user_intent=UserIntent.PROVIDING_EVIDENCE,
    rationale="User proactively provided GC logs showing frequent full GCs and high heap usage, "
              "classic memory leak pattern. Strongly supports memory leak hypothesis even though "
              "we didn't specifically request GC logs yet.",
)

# Evidence created and linked normally
# Agent responds: "Thank you for the GC logs! This is excellent evidence..."
```

### 11.3 Blocked Evidence Handling

```python
# User reports cannot access critical evidence

User: "I don't have access to the production database to run that query"

# ===================================================================
# Classification
# ===================================================================

classification = EvidenceClassification(
    matched_requirement_ids=["req_001"],  # Pool metrics requirement
    relevant_hypothesis_ids=[],
    hypothesis_support={},
    completeness=CompletenessLevel.PARTIAL,
    completeness_score=0.0,
    form=EvidenceForm.USER_INPUT,
    evidence_type=EvidenceStance.NEUTRAL,
    user_intent=UserIntent.REPORTING_UNAVAILABLE,
    rationale="User reports no access to production database",
    follow_up_needed="Explore alternative evidence sources or escalate for access",
)

# ===================================================================
# Lifecycle Update
# ===================================================================

req_001.status = EvidenceRequirementStatus.BLOCKED
req_001.blocked_reason = "User lacks production database access"
req_001.updated_at_turn = 10

# ===================================================================
# Agent Response (Alternative Paths)
# ===================================================================

agent_response = """
I understand you don't have production database access. Let's explore alternatives:

**Alternative 1**: Can someone else on your team run the query?
  - DBA or DevOps with production access
  - Share the query: `psql -c 'SELECT count(*) FROM pg_stat_activity'`

**Alternative 2**: Is there a monitoring dashboard showing connection pool metrics?
  - Check APM tool (New Relic, Datadog)
  - Database monitoring tools

**Alternative 3**: Can we infer from application logs?
  - Look for "connection timeout" errors
  - Frequency suggests pool exhaustion

If none are available, we can:
- Continue investigation with lower confidence
- Escalate to team with access
- Focus on other evidence to narrow down cause

What would you prefer?
"""
```

---

## 12. Appendix

### 12.1 Complete Schema Reference

See sections 3 and 4 for complete schemas:
- Evidence
- Hypothesis
- EvidenceLink
- EvidenceRequirement
- AcquisitionGuidance
- EvidenceClassification
- InvestigationMode

### 12.2 Enum Reference

```python
# Evidence-related enums
EvidenceSourceType: log_file | metrics_data | trace_data | database_query | command_output | code_review | config_file | screenshot | api_response | user_observation

EvidenceForm: user_input | document

EvidenceStance: strongly_supports | supports | neutral | refutes | irrelevant

HypothesisStatus: proposed | testing | validated | refuted

EvidenceRequirementPriority: critical | important | optional

EvidenceRequirementStatus: pending | partial | complete | blocked | obsolete

UserIntent: providing_evidence | asking_question | reporting_unavailable | reporting_status | clarifying | off_topic

CompletenessLevel: partial | complete | over_complete

InvestigationStrategy: active_incident | post_mortem
```

### 12.3 Database Indexes

```sql
-- Evidence table
CREATE INDEX idx_evidence_case_id ON evidence(case_id);
CREATE INDEX idx_evidence_collected_at ON evidence(collected_at DESC);
CREATE INDEX idx_evidence_source_type ON evidence(source_type);
CREATE INDEX idx_evidence_form ON evidence(form);

-- Hypotheses table
CREATE INDEX idx_hypotheses_case_status ON hypotheses(case_id, status);
CREATE INDEX idx_hypotheses_status ON hypotheses(status);

-- JSONB indexes for nested queries
CREATE INDEX idx_hypotheses_evidence_links ON hypotheses USING GIN (evidence_links);
CREATE INDEX idx_hypotheses_requirements ON hypotheses USING GIN ((evidence_requirements::jsonb));
```

### 12.4 Token Budget Impact

```
Evidence Summary in Prompt Context (Layer 5):

Per Evidence (in context):
- Source type: ~15 chars
- Summary: ~100 chars (avg)
- Total: ~115 chars = ~30 tokens

Top 5 Evidence: 5 × 30 = 150 tokens

vs. Loading Raw Data:
50KB log file = ~12,500 tokens (83x more)

Evidence Requirements (Layer 4):

Per Requirement (in prompt):
- Description: ~100 chars
- Tests aspect: ~50 chars
- Priority: ~10 chars
- Commands: ~150 chars
- Total: ~310 chars = ~80 tokens

3 Requirements: 3 × 80 = 240 tokens
```

### 12.5 Cross-Reference Index

**Referenced from Investigation State and Control Framework**:
- Investigation phases (0-6)
- OODA loop mechanics
- Engagement modes (Consultant, Lead Investigator)
- Phase transition logic
- Working conclusion structure
- Progress tracking

**Referenced from Prompt Engineering Architecture**:
- Multi-layer prompt assembly
- Phase-specific prompt templates
- Context management (Layer 4, Layer 5)
- Token optimization strategies
- Agent behavior specifications

**Referenced by Other Components**:
- Preprocessing pipeline (Evidence.summary generation)
- LLM client (Classification, Hypothesis analysis)
- Storage service (S3 for raw artifacts, DB for summaries)

---

**END OF DOCUMENT**

**Version**: 1.1 (Enhanced)  
**Date**: 2025-11-01  
**Status**: Production Ready  
**Architecture**: Two-Layer (Fact Core + Collection Workflow)  
**Integration**: Works with Investigation State Framework + Prompt Engineering Architecture  
**Reasoning Model**: Qualitative (Status-Based, No Numeric Confidence)  
**Storage**: Two-Tier (DB Summaries + S3 Artifacts)  
**Safety**: Command validation, read-only preferred, dangerous pattern blocking