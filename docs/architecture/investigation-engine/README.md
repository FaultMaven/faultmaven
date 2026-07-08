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
- **[Two-Dimensional Hypothesis Methodology](./two-dimensional-hypothesis-methodology.md)** — The diagnostic reasoning methodology beneath the hypothesis lifecycle: forming candidate root causes (signature screening, family completeness), structuring them as causal chains on a 2D roadmap (OR roots / AND rungs), invalidation-first search (info-per-cost, intersection pruning), and the two grades of root-cause validation (mechanistic → treatment, counterfactual → resolved). Expands [Framework §6](./evidence-driven-investigation-framework.md#6-hypothesis-model).
- **[Evidence Needs Design](./evidence-needs-design.md)** — Demand-side pool of outstanding evidence asks: creation triggers, lifecycle, engine backstop, context block, suggestion linkage. §11 is the as-built map (file:line, metrics, wire seam) for debugging the shipped feature.
- **[Choice-Response Resolution](./choice-response-resolution.md)** — Resolving a user's response (clicked or typed) to an agent-offered choice: bounded choice classifier, hypothesis-action routing, resolution-readiness gate. *(Formerly "Intent Resolution".)*
- **[Investigation Journal](./investigation-journal.md)** — Append-only long-term memory for key findings
- **[Progress Transparency](./progress-transparency.md)** — Progress monitoring, repair patterns, milestone dependencies
- **[Insufficient-Evidence Handling](./insufficient-evidence-handling.md)** — What the engine does when no cause can be grounded from available data: the advisor / structured-handoff posture, the two layers (hypothesis-layer stall signals + causal-graph-layer assurance) that currently carry it, and the direction toward a single computed *verification status*. Standardizes terminology (assessment variable vs disposition, hypothesis layer vs causal-graph layer, enforcement tier).
- **[Orchestration Capabilities](./orchestration-capabilities.md)** — Checkpointing, streaming, DA tool loop
- **[Error Handling and Recovery](./error-handling-and-recovery.md)** — Error patterns, recovery strategies, diagnostic reasoning validation

## Related

Evidence classification, flow, and preprocessing are in [Data Processing](../data-processing/).
