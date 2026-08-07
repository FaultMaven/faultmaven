# Reference Documentation

Factual lookup materials for FaultMaven developers and operators.

## Sections

| Section | Description |
|---------|-------------|
| [API](./api/) | OpenAPI specification |
| [Configuration](./configuration/) | Infrastructure and service configuration options |
| [Database](./database/) | Database schema and migration reference |
| [Tools](./tools/) | Investigation tool catalog and developer guide |

## Quick Links

- [OpenAPI Spec](./api/openapi.json) — Generated from the running app; CI fails if it drifts
- [API Reference](./api/README.md) — The same spec rendered for reading, including per-operation auth
- [LLM Model Capabilities](./llm-model-capabilities.md)
- [Local LLM Setup](./configuration/local-llm-setup.md)
- [Tool Catalog](./tools/tool-catalog.md)

## Reference vs Architecture vs Guides

| Type | Purpose | Example |
|------|---------|---------|
| **Reference** (here) | Information-oriented, factual lookup | API spec, config options, tool catalog |
| **Architecture** | Explanation-oriented, design understanding | How the investigation engine works |
| **Guides** | Task-oriented, practical steps | How to add a custom LLM provider |

- Learning how to use FaultMaven → [Getting Started](../getting-started/)
- Performing a specific task → [Guides](../guides/)
- Understanding system concepts → [Architecture](../architecture/)
