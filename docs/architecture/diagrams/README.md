# FaultMaven Architecture Diagrams

Mermaid source for FaultMaven's architecture diagrams. Viewed best in
[Mermaid Live Editor](https://mermaid.live), VS Code's Mermaid preview,
or directly on GitHub (which renders `.mmd` inline).

## Diagrams

- **[system-architecture.mmd](./system-architecture.mmd)** — primary one-screen
  runtime view. Covers external actors → transport → modular monolith →
  investigation engine → infrastructure → storage → external services, with
  the composition root as the wiring node.
- **[DI-diagram.mmd](./DI-diagram.mmd)** — zoom on dependency injection.
  Shows how Settings feeds the three provider modules (infrastructure, tools,
  services), which are composed at startup in `main.py` lifespan and attached
  to `app.state`.

## Regenerating to PNG/SVG

```bash
# Requires node; installs @mermaid-js/mermaid-cli on demand
npx --yes @mermaid-js/mermaid-cli \
  -i docs/architecture/diagrams/system-architecture.mmd \
  -o /tmp/system-architecture.png \
  -w 2000 -H 2400
```

## Related

- [Architecture Overview](../architecture-overview.md) — prose companion to
  the primary diagram
- [Module Organization](../core-architecture/module-organization-design.md) —
  vertical vs domain-service classification
- [ER Diagram](../data-and-storage/er-diagram.md) — auto-generated from
  SQLAlchemy models (separate tool: `scripts/generate_er_diagram.py`)
