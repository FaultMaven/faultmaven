# Investigation Engine

Documentation for FaultMaven's core investigation framework.

## Reading Order

For understanding the investigation system, read in this order:

1. **[Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md)** — The framework: philosophy, milestones, opportunistic completion
2. **[Investigation Lifecycle Logic](./investigation-lifecycle-logic.md)** — State transitions, stage routing, case actions, turn tracking
3. **[Investigation Data Models](./investigation-data-models.md)** — CaseStatus, Evidence, Hypothesis, Solution, and related structures
4. **[Prompt Templates](./prompt-templates.md)** — INQUIRY / INVESTIGATING / TERMINAL templates and their assembly
5. **[Agent Behavioral Rules](./agent-behavioral-rules.md)** — 6 prompt-injected rules that constrain agent output

## Reference

- **[Intent Resolution](./intent-resolution.md)** — Bounded choice matching, hypothesis action routing
- **[Investigation Journal](./investigation-journal.md)** — Append-only long-term memory for key findings
- **[Progress Transparency](./progress-transparency.md)** — Progress monitoring, repair patterns, milestone dependencies
- **[Orchestration Capabilities](./orchestration-capabilities.md)** — Checkpointing, streaming, DA tool loop
- **[Error Handling and Recovery](./error-handling-and-recovery.md)** — Error patterns, recovery strategies, diagnostic reasoning validation

## Related

Evidence classification, flow, and preprocessing are in [Data Processing](../data-processing/).
