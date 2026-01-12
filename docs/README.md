# FaultMaven Documentation

Welcome to the FaultMaven documentation.

## Documentation Structure

| Section | Description | Audience |
|---------|-------------|----------|
| [Getting Started](./getting-started/) | Installation, quickstart, user guide | New users |
| [Guides](./guides/) | Task-oriented how-to guides | All users |
| [Architecture](./architecture/) | System design, decisions, specifications | Developers |
| [Reference](./reference/) | API, configuration, tools, database | Developers |
| [Development](./development/) | Testing, standards, contribution | Contributors |
| [Operations](./operations/) | Monitoring, security, runbooks | Operators |

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
| [Case and Session Concepts](./architecture/case-and-session-concepts.md) | Core domain concepts |
| [Investigation Framework](./architecture/milestone-based-investigation-framework.md) | OODA investigation process |
| [System Requirements](./reference/system-requirements.md) | Functional requirements |

## Contributing

- **[Contributing Guide](./CONTRIBUTING.md)** - How to contribute
- **[Code of Conduct](./CODE_OF_CONDUCT.md)** - Community standards
- **[Development Setup](./development/)** - Environment configuration

## Directory Overview

```
docs/
├── getting-started/     # New user onboarding
├── guides/              # How-to guides
├── architecture/        # System design & ADRs
│   ├── decisions/       # Architecture Decision Records
│   ├── diagrams/        # Visual diagrams
│   ├── reference/       # Detailed specifications
│   └── specifications/  # Formal specs
├── reference/           # Technical reference
│   ├── api/             # OpenAPI specification
│   ├── configuration/   # Config reference
│   ├── database/        # Schema documentation
│   └── tools/           # Tool catalog
├── development/         # Contributor docs
└── operations/          # Production operations
    ├── monitoring/      # Logging & metrics
    ├── security/        # Security guides
    └── runbooks/        # Troubleshooting
```

> **Note**: Local working documents can be stored in `docs/_archive/` (not tracked in git).

## Naming Conventions

- **Folder names**: lowercase with hyphens (e.g., `getting-started/`)
- **File names**: lowercase with hyphens (e.g., `user-guide.md`)
- **README.md**: Index file for each folder (uppercase is standard)

---

**Last Updated**: 2026-01-12
