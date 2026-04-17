# FaultMaven Architecture Documentation

Master index for architecture documentation.

## Canonical Documents (Source of Truth)

These four documents are the authoritative source for their respective domains. Other docs defer to them on conflicts.

| Document | Covers |
|----------|--------|
| **[Architectural Design Principles](core-architecture/architectural-design-principles.md)** | The 12 design principles (deployment-agnostic, vertical modules, composition root, interfaces, observability) |
| **[Investigation Lifecycle Logic](investigation-engine/investigation-lifecycle-logic.md)** | Case actions, state transitions, stage routing, turn tracking |
| **[Agent Behavioral Rules](investigation-engine/agent-behavioral-rules.md)** | 6 prompt-injected rules that constrain agent output |
| **[Knowledge Base Architecture](knowledge-and-ai/knowledge-base-architecture.md)** | 3-tier KB, storage, retrieval, federated search |

## Entry Point

- **[Architecture Overview](./architecture-overview.md)** — System-wide tour, navigation into subsystems

## Sections

| Section | Focus |
|---------|-------|
| [core-architecture/](./core-architecture/) | Design principles, module organization, structured output |
| [investigation-engine/](./investigation-engine/) | Milestone-based investigation framework, prompts, rules |
| [knowledge-and-ai/](./knowledge-and-ai/) | KB architecture, runbook content, vector retrieval |
| [data-and-storage/](./data-and-storage/) | Database schemas, repository pattern, ER diagram |
| [data-processing/](./data-processing/) | Evidence classification, preprocessing, extractors |
| [case-and-session/](./case-and-session/) | Case concepts, evidence store |
| [security/](./security/) | IAM, PII redaction, OAuth |
| [api-and-integration/](./api-and-integration/) | API mapping |
| [decisions/](./decisions/) | Architecture Decision Records |
| [diagrams/](./diagrams/) | Mermaid diagram sources |
| [specifications/](./specifications/) | Formal specs (currently: LLM configuration design) |

## How to Navigate

- **New to FaultMaven?** → Start with [Architecture Overview](./architecture-overview.md)
- **Understanding an investigation turn?** → [Investigation Lifecycle Logic](investigation-engine/investigation-lifecycle-logic.md)
- **Writing or changing a prompt?** → [Prompt Templates](investigation-engine/prompt-templates.md) + [Agent Behavioral Rules](investigation-engine/agent-behavioral-rules.md)
- **Touching module boundaries?** → [Module Organization Design](core-architecture/module-organization-design.md) + `.importlinter`
- **Schema questions?** → [data-and-storage/schemas/](data-and-storage/schemas/) + `alembic/versions/`
