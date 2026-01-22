# Reference Documentation

Technical reference materials for FaultMaven developers and operators.

## Overview

This section contains detailed technical reference documentation organized by type:

- **API** - REST API specifications and contracts
- **Configuration** - System configuration reference
- **Database** - Schema documentation and migrations
- **Tools** - Investigation tools catalog
- **Deep Dives** - Detailed architectural analysis and design documents

## Sections

| Section | Description |
|---------|-------------|
| [API](./api/) | OpenAPI specification and REST API reference |
| [Configuration](./configuration/) | Infrastructure and service configuration |
| [Database](./database/) | Database schema, migrations, and procedures |
| [Tools](./tools/) | Tool catalog and custom tool development |
| [Deep Dives](./deep-dives/) | Detailed architectural analysis and design |

## Quick Reference

### System Overview

| Document | Description |
|----------|-------------|
| [System Requirements](./system-requirements.md) | Functional and non-functional requirements |

## API

- **[OpenAPI Spec](./api/openapi.locked.yaml)** - API specification

## Configuration

- **[Local LLM Setup](./configuration/local-llm-setup.md)** - Ollama, vLLM configuration
- **[KB Metadata Persistence](./configuration/kb-metadata-persistence.md)** - Knowledge base storage

## Database

- **[Schema Files](./database/)** - SQL migration scripts

## Tools

- **[Tool Catalog](./tools/tool-catalog.md)** - Available investigation tools
- **[Developer Guide](./tools/developer-guide.md)** - Building custom tools

## Deep Dives

Comprehensive architectural analysis and detailed design documents moved from `architecture/reference/`:

- **[Agentic Framework Architecture](./deep-dives/agentic-framework-architecture.md)** - Multi-agent system design
- **[Architectural Layers](./deep-dives/architectural-layers.md)** - System layer architecture
- **[Case Agent Integration Design](./deep-dives/case-agent-integration-design.md)** - Agent-case integration
- **[Case Persistence System Design](./deep-dives/case-persistence-system-design.md)** - Case storage architecture
- **[Component Interactions](./deep-dives/component-interactions.md)** - Inter-component communication
- **[Context Engineering Analysis](./deep-dives/context-engineering-analysis.md)** - LLM context optimization
- **[Conversational Interaction Model](./deep-dives/conversational-interaction-model-design.md)** - User interaction design
- **[FaultMaven System Detailed Design](./deep-dives/faultmaven-system-detailed-design.md)** - Complete system design
- **[Infrastructure Layer Guide](./deep-dives/infrastructure-layer-guide.md)** - Infrastructure patterns

## Reference vs Architecture vs Guides

Understanding the difference between reference documentation and other doc types:

| Type | Purpose | Example |
|------|---------|---------|
| **Reference** (here) | Information-oriented, factual lookup | API spec, config options, tool catalog |
| **Deep Dives** (here) | Detailed technical analysis and design | System design documents, architectural analysis |
| **Architecture** | Explanation-oriented, understanding | How the investigation engine works conceptually |
| **Guides** | Task-oriented, practical steps | How to add a custom LLM provider |

**When to use reference docs:**
- Looking up API endpoints and parameters
- Finding configuration options
- Understanding database schema
- Checking available tools
- Deep technical analysis of design decisions

**When NOT to use reference docs:**
- Learning how to use FaultMaven → See [Getting Started](../getting-started/)
- Performing a specific task → See [Guides](../guides/)
- Understanding system concepts → See [Architecture](../architecture/)
