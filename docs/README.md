# FaultMaven Documentation

Welcome to the FaultMaven documentation.

## Documentation Structure

This documentation follows the [Diátaxis framework](https://diataxis.fr/), organizing content by user needs:

| Section | Diátaxis Type | Description | Audience |
|---------|---------------|-------------|----------|
| [Getting Started](./getting-started/) | **Tutorials** | Step-by-step learning paths | New users |
| [Guides](./guides/) | **How-To Guides** | Task-oriented practical guides | All users |
| [Architecture](./architecture/) | **Explanation** | System design, decisions, concepts | Developers |
| [Reference](./reference/) | **Reference** | API, configuration, tools, database | Developers |
| [Development](./development/) | **How-To + Reference** | Testing, standards, contribution | Contributors |
| [Operations](./operations/) | **How-To + Reference** | Monitoring, security, runbooks | Operators |

## Quick Links

### New Users

1. **[Quickstart](./getting-started/quickstart.md)** - Get running in 5 minutes
2. **[Installation](./getting-started/installation.md)** - Detailed setup guide
3. **[User Guide](./getting-started/user-guide.md)** - Complete user documentation

### Developers

- **[Architecture Overview](./architecture/architecture-overview.md)** - System design
- **[Environment Variables](./development/environment-variables.md)** - Configuration
- **[Adding LLM Providers](./guides/adding-llm-providers.md)** - Provider integration
- **[API Specification](./reference/api/)** - OpenAPI spec

### Operators

- **[Security](./operations/security/)** - RBAC, PII protection
- **[Monitoring](./operations/monitoring/)** - Logging, observability
- **[Runbooks](./operations/runbooks/)** - Troubleshooting guides

## Key Documents

| Document | Description |
|----------|-------------|
| [Architecture Overview](./architecture/architecture-overview.md) | Master system design |
| [Case and Session Concepts](./architecture/case-and-session/case-and-session-concepts.md) | Core domain concepts |
| [Investigation Framework](./architecture/investigation-engine/milestone-based-investigation-framework.md) | OODA investigation process |
| [Testing Standards](./development/testing/standards.md) | Testing requirements and standards |

## Contributing

- **[Contributing Guide](./CONTRIBUTING.md)** - How to contribute
- **[Code of Conduct](./CODE_OF_CONDUCT.md)** - Community standards
- **[Development Setup](./development/)** - Environment configuration

## Directory Overview

```
docs/
├── getting-started/     # New user onboarding (tutorials)
├── guides/              # Task-oriented how-to guides
├── architecture/        # System design & explanations
│   ├── api-and-integration/      # API layer & module mapping
│   ├── case-and-session/         # Case lifecycle & sessions
│   ├── core-architecture/        # Foundational patterns & DI
│   ├── data-and-storage/         # Persistence layer
│   ├── data-processing/          # File handling & preprocessing
│   ├── investigation-engine/     # Investigation workflows
│   ├── knowledge-and-ai/         # Knowledge base & vectors
│   ├── security/                 # Authentication & security
│   ├── decisions/                # Architecture Decision Records (ADRs)
│   ├── diagrams/                 # Visual architecture diagrams
│   └── specifications/           # Formal specifications
├── reference/           # Technical reference materials
│   ├── api/             # OpenAPI specification
│   ├── configuration/   # Configuration reference
│   ├── database/        # Schema documentation
│   ├── deep-dives/      # Detailed architectural deep-dives
│   └── tools/           # Tool catalog
├── development/         # Contributor documentation
│   └── testing/         # Testing standards & patterns
└── operations/          # Production operations
    ├── monitoring/      # Logging & metrics
    ├── security/        # Security guides
    └── runbooks/        # Troubleshooting procedures
```

## Where to Put New Documentation

When creating new documentation, use this guide to determine the appropriate location:

| Document Type | Location | Example |
|--------------|----------|---------|
| Step-by-step tutorial for beginners | `getting-started/` | "Your First Investigation" |
| Task-oriented practical guide | `guides/` | "How to Add a Custom LLM Provider" |
| System design explanation | `architecture/[domain]/` | "Investigation Engine Architecture" |
| Architecture decision record | `architecture/decisions/` | "ADR-002: Event Sourcing Strategy" |
| Visual architecture diagram | `architecture/diagrams/` | "System Context Diagram" |
| Formal specification | `architecture/specifications/` | "Configuration Management Spec" |
| API documentation | `reference/api/` | "REST API Endpoints" |
| Configuration options | `reference/configuration/` | "Environment Variables Reference" |
| Deep architectural analysis | `reference/deep-dives/` | "Context Engineering Analysis" |
| Testing guide or pattern | `development/testing/` | "Async Testing Patterns" |
| Development standard | `development/` | "Datetime Standard" |
| Operational runbook | `operations/runbooks/` | "Database Recovery Procedure" |
| Security guide | `operations/security/` | "API Key Security" |

**General Rules**:
- **Tutorials** (learning-oriented) → `getting-started/`
- **How-to guides** (task-oriented) → `guides/` or domain-specific
- **Explanations** (understanding-oriented) → `architecture/`
- **Reference** (information-oriented) → `reference/`

## Naming Conventions

- **Folder names**: lowercase with hyphens (e.g., `getting-started/`)
- **File names**: lowercase with hyphens (e.g., `user-guide.md`)
- **README.md**: Index file for each folder (uppercase is standard)

---

**Last Updated**: 2026-01-22
