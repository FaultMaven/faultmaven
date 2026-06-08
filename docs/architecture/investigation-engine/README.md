# Investigation Engine

Documentation for FaultMaven's core investigation framework.

## Reading Order

For understanding the investigation system, read in this order:

1. **[Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md)** — The framework: philosophy, milestones, opportunistic completion
2. **[Investigation Lifecycle Logic](./investigation-lifecycle-logic.md)** — State transitions, stage routing, case actions, turn tracking
3. **[Investigation Data Models](./investigation-data-models.md)** — CaseState, Evidence, Hypothesis, Solution, and related structures
4. **[Prompt Assembly Architecture](./prompt-assembly-architecture.md)** — How prompts are assembled: the three-template system, shared constants, dispatch (`get_prompt_for_case`), and audit invariants. (Prompt *text* lives in `templates.py`; stage duties in `agent-stage-playbook.md`, behavioral rules in `agent-behavioral-rules.md`.)
5. **[Agent Behavioral Rules](./agent-behavioral-rules.md)** — 8 prompt-injected rules that constrain agent output and shape input reading

> The unified opportunistic flow (formerly proposed as "Investigation Flow Redesign") shipped 2026-06-05 and is folded into [Investigation Lifecycle Logic §2](./investigation-lifecycle-logic.md#2-mitigation-as-an-insert) — design rationale, assessment-vs-gate variables, and resolved decisions (§2.5) included.

## Reference

- **[Investigation Invariant Enforcement Matrix](./investigation-invariants.md)** — The lifecycle invariant registry (INV-01 … INV-24): enforcement tiers, pinning tests, and drift notes. Extracted from the lifecycle doc so it can be audited standalone.
- **[Runbook Cause Matching](./runbook-cause-matching.md)** — Evaluating runbook Cause Indicators against case state (deterministic predicates + `case_evidence_qa` fallback) to attribute the active Cause. *(Formerly "Indicator Resolution".)*
- **[Evidence Needs Design](./evidence-needs-design.md)** — Demand-side pool of outstanding evidence asks: creation triggers, lifecycle, engine backstop, context block, suggestion linkage. §11 is the as-built map (file:line, metrics, wire seam) for debugging the shipped feature.
- **[Choice-Response Resolution](./choice-response-resolution.md)** — Resolving a user's response (clicked or typed) to an agent-offered choice: bounded choice classifier, hypothesis-action routing, resolution-readiness gate. *(Formerly "Intent Resolution".)*
- **[Investigation Journal](./investigation-journal.md)** — Append-only long-term memory for key findings
- **[Progress Transparency](./progress-transparency.md)** — Progress monitoring, repair patterns, milestone dependencies
- **[Orchestration Capabilities](./orchestration-capabilities.md)** — Checkpointing, streaming, DA tool loop
- **[Error Handling and Recovery](./error-handling-and-recovery.md)** — Error patterns, recovery strategies, diagnostic reasoning validation

## Related

Evidence classification, flow, and preprocessing are in [Data Processing](../data-processing/).
