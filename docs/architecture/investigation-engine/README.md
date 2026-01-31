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

- **[Error Handling and Recovery](./error-handling-and-recovery.md)** - Error handling patterns and recovery strategies

## Purpose

This section covers FaultMaven's investigation engine - the opportunistic framework that guides AI agents through problem diagnosis and resolution, including prompt engineering and context management.

## Key Concepts

- **Opportunistic completion**: Agent completes multiple milestones in one turn if data allows
- **4 case statuses**: INQUIRY, INVESTIGATING, RESOLVED, CLOSED
- **3 user-facing stages**: UNDERSTANDING, DIAGNOSING, RESOLVING
- **4 internal stages**: SYMPTOM_VERIFICATION, HYPOTHESIS_FORMULATION, HYPOTHESIS_VALIDATION, SOLUTION
- **2 investigation paths**: MITIGATION_FIRST (1-4-2-3-4) and ROOT_CAUSE (1-2-3-4)
- **Three-template prompt system**: Context, instructions, and reasoning templates
