# Investigation Engine

Documentation for FaultMaven's core investigation framework and AI-driven problem-solving architecture.

## Documents

### Core Architecture

- **[Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md)** - Overview and philosophy of the evidence-driven investigation approach
- **[Investigation Data Models](./investigation-data-models.md)** - Core data structures (CaseStatus, Evidence, Hypothesis, Solution, ProposedAction, etc.)
- **[Investigation Lifecycle Logic](./investigation-lifecycle-logic.md)** - Case actions, path routing, and turn tracking

### Prompt Engineering

- **[Prompt Templates](./prompt-templates.md)** - Implementation-ready prompt templates and three-template system

### Operations

- **[Orchestration Capabilities](./orchestration-capabilities.md)** - State Checkpointing, Time Travel, HIL, and Streaming
- **[Error Handling and Recovery](./error-handling-and-recovery.md)** - Error handling patterns and recovery strategies
- **[Implementation Gap Analysis](./implementation-gap-analysis.md)** - Design vs implementation alignment tracker

### Deprecated

- **[Prompt Engineering Guide](./prompt-engineering-guide.md)** - Deprecated (old 4-stage model). See Prompt Templates instead.

### Evidence Documentation (See Data Processing)

Evidence classification, flow, and preprocessing are documented in the [Data Processing](../data-processing/) section:

- **[Evidence Classification Design](../data-processing/evidence-classification-design.md)** - Evidence taxonomy, categories, and DataType enum
- **[Evidence Flow Architecture](../data-processing/evidence-flow-architecture.md)** - System architecture and flow diagrams
- **[Evidence Failure Modes](../data-processing/evidence-failure-modes.md)** - Failure handling for single-phase creation
- **[Data Preprocessing Design](../data-processing/data-preprocessing-design-specification.md)** - Three-tier preprocessing model

## Implementation Status

| Component | Status | Notes |
|-----------|--------|-------|
| Evidence-Driven Investigation Framework | Implemented | 2-stage model with mitigation detour (DIAGNOSIS, TREATMENT + MITIGATION detour) operational |
| Investigation Data Models | Implemented | All core models in production (gate milestones + progress milestones) |
| Investigation Lifecycle Logic | Implemented | Case actions, inference-based stage transitions, path routing |
| Prompt Engineering System | Implemented | Three-template system (DIAGNOSIS, MITIGATION, TREATMENT prompts) |
| Error Handling and Recovery | Implemented | LLM retry, stagnation detection, compliance detection, degraded mode |
| Orchestration: Checkpointing/Time-Travel | Design Complete | `CaseCheckpoint` model defined, not instantiated |
| Knowledge Fast-Track Resolution | Design Complete | Data model exists, milestone engine wiring deferred |
| `solution_verified` Evidence Validation | Design Complete | User-Agent Handshake handles transition, no evidence check |

See [Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md) for full design details.

---

## Purpose

This section covers FaultMaven's investigation engine — the evidence-driven framework that guides AI agents through problem diagnosis and resolution, including prompt engineering and context management.

## Key Concepts

- **Evidence-driven investigation**: Agent processes evidence naturally within the current stage; transitions happen when the user acts
- **4 case statuses**: INQUIRY (phase), INVESTIGATING (phase), RESOLVED (disposition), CLOSED (disposition)
- **2 core stages + mitigation detour**: DIAGNOSIS (understand & diagnose), TREATMENT (permanent fix & resolution), with an optional MITIGATION detour (temp fix)
- **3 user-facing stage names**: "Diagnosing", "Mitigating", "Resolving"
- **10 investigation milestones**: 4 gate milestones (drive transitions) + 6 progress milestones (LLM context)
- **Inference-based transitions**: User compliance with proposed actions triggers stage transitions
- **2 investigation paths**: MITIGATION_FIRST (DIAGNOSIS → MITIGATION → DIAGNOSIS → TREATMENT) and ROOT_CAUSE (DIAGNOSIS → TREATMENT)
- **Three-template prompt system**: DIAGNOSIS, MITIGATION, and TREATMENT stage instructions
