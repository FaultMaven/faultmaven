# Investigation Engine

Documentation for FaultMaven's core investigation framework and AI-driven problem-solving architecture.

## Documents

### Core Architecture

- **[Opportunistic Investigation Framework](./opportunistic-investigation-framework.md)** - Overview and philosophy of the opportunistic investigation approach
- **[Investigation Data Models](./investigation-data-models.md)** - Core data structures (CaseStatus, Evidence, Hypothesis, Solution, etc.)
- **[Investigation Lifecycle Logic](./investigation-lifecycle-logic.md)** - State transitions, path routing, and turn tracking

### Prompt Engineering

- **[Prompt Engineering Guide](./prompt-engineering-guide.md)** - Three-template prompt system and LLM interaction patterns
- **[Prompt Templates](./prompt-templates.md)** - Implementation-ready prompt templates
- **[Prompt Implementation Examples](./prompt-implementation-examples.md)** - Complete integration code examples

### Operations

- **[Orchestration Capabilities](./orchestration-capabilities.md)** - State Checkpointing, Time Travel, HIL, and Streaming
- **[Error Handling and Recovery](./error-handling-and-recovery.md)** - Error handling patterns and recovery strategies

### Evidence Documentation (See Data Processing)

Evidence classification, flow, and preprocessing are documented in the [Data Processing](../data-processing/) section:

- **[Evidence Classification Design](../data-processing/evidence-classification-design.md)** - Evidence taxonomy, categories, and DataType enum
- **[Evidence Flow Architecture](../data-processing/evidence-flow-architecture.md)** - System architecture and flow diagrams
- **[Evidence Failure Modes](../data-processing/evidence-failure-modes.md)** - Failure handling for single-phase creation
- **[Data Preprocessing Design](../data-processing/data-preprocessing-design-specification.md)** - Three-tier preprocessing model

### Historical Documents

- **[Workflow Design Review](./workflow-design-review.md)** - Historical design decisions (2026-02-09) - Some decisions superseded by later evidence model changes

## Purpose

This section covers FaultMaven's investigation engine - the opportunistic framework that guides AI agents through problem diagnosis and resolution, including prompt engineering and context management.

## Key Concepts

- **Opportunistic completion**: Agent completes multiple milestones in one turn if data allows
- **4 case statuses**: INQUIRY, INVESTIGATING, RESOLVED, CLOSED
- **3 user-facing stages**: UNDERSTANDING, DIAGNOSING, RESOLVING
- **4 internal stages**: SYMPTOM_VERIFICATION, HYPOTHESIS_FORMULATION, HYPOTHESIS_VALIDATION, SOLUTION
- **2 investigation paths**: MITIGATION_FIRST (1-4-2-3-4) and ROOT_CAUSE (1-2-3-4)
- **Three-template prompt system**: Context, instructions, and reasoning templates
