# Core Architecture

Foundational patterns and system design for FaultMaven.

## Documents

- **[Architectural Design Principles](./architectural-design-principles.md)** — The 12 canonical principles (deployment-agnostic, vertical modules, composition root, interfaces, observability, etc.)
- **[Module Organization Design](./module-organization-design.md)** — Vertical module vs horizontal layer classification criteria, Domain Service patterns, and the schema-verified module roster
- **[Structured Output Capability System](./structured-output-capability-system.md)** — Provider-agnostic LLM structured output design
- **[Infrastructure Layer Guide](./infrastructure-layer-guide.md)** — Infrastructure layer conventions

> The previous "Vertical vs Layer Structuring" memo has been archived (see [`docs/archive/2026/04/vertical-vs-layer-structuring-explained.md`](../../archive/2026/04/vertical-vs-layer-structuring-explained.md)). Its guidance is now consolidated into Module Organization Design.

## Where Did the Other Docs Go?

- **Dependency injection design** is now covered by **Principle 5 (Composition Root)** in [architectural-design-principles.md](./architectural-design-principles.md). The container implementation is in `faultmaven/container/`.
- **Interface-based design** is covered by **Principle 4** in the principles doc.
- **Service patterns** are covered by **Principles 2, 5, 6** and [module-organization-design.md](./module-organization-design.md).
