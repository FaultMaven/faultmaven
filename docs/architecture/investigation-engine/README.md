# Investigation Engine

Documentation for FaultMaven's core investigation framework and AI-driven problem-solving architecture.

## Documents

- **[Error Handling and Recovery](./error-handling-and-recovery.md)** - Error handling patterns and recovery strategies
- **[Milestone-Based Investigation Framework](./milestone-based-investigation-framework.md)** - Core investigation lifecycle (v2.0 architecture)
- **[Phase-Based Retrieval](./phase-based-retrieval.md)** - Context retrieval strategies by investigation phase
- **[Prompt Engineering Guide](./prompt-engineering-guide.md)** - Three-template prompt system and LLM interaction patterns
- **[Prompt Implementation Examples](./prompt-implementation-examples.md)** - Complete integration code examples for prompts
- **[Prompt Templates](./prompt-templates.md)** - Implementation-ready prompt templates

## Purpose

This section covers FaultMaven's investigation engine - the milestone-based framework that guides AI agents through problem diagnosis and resolution, including prompt engineering, context management, and phase-based reasoning.

## Key Concepts

- **Milestone-based completion**: Agent completes multiple milestones in one turn if data allows
- **4 case statuses**: CONSULTING, INVESTIGATING, RESOLVED, CLOSED
- **3 investigation stages**: UNDERSTANDING, DIAGNOSING, RESOLVING
- **Three-template prompt system**: Context, instructions, and reasoning templates
