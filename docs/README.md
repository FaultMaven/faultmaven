# FaultMaven Documentation

Master index for all FaultMaven documentation.

> **📝 Recent Update (2025-10-24):** Session-Case architecture refactored to v2.0 spec.
> See **[case-and-session-concepts.md](./architecture/case-and-session-concepts.md)** for the authoritative specification.

## Quick Navigation

- 🚀 **[Getting Started](./getting-started/)** - Installation, quickstart, user guide
- 🏗️ **[Architecture](./architecture/architecture-overview.md)** - System architecture, design, and specifications
- 🔌 **[API Documentation](./api/)** - API contracts, OpenAPI spec, integration guides
- 🛠️ **[Tools](./tools/)** - Session-level tools for troubleshooting (KB search, web search, log analysis, MCP)
- 💻 **[Development](./development/)** - Developer environment setup and configuration
- 🏗️ **[Infrastructure](./infrastructure/)** - Infrastructure setup (Redis, ChromaDB, LLM providers, Opik)
- 🧪 **[Testing](./testing/)** - Testing strategies, patterns, and guides
- 📚 **[How-To Guides](./how-to/)** - Integration guides and operational procedures
- 🔒 **[Security](./security/)** - Security implementation, RBAC, and policies
- 📝 **[Logging](./logging/)** - Logging architecture and configuration
- 🎨 **[Frontend](./frontend/)** - Frontend documentation (website + copilot browser extension)
- 📖 **[Runbooks](./runbooks/)** - Operational runbooks for common issues
- 📦 **[Archive](./archive/)** - Historical documentation (migrations, obsolete designs, release notes)

---

## Documentation Structure

### Primary Documents

1. **[Architecture Overview](./architecture/architecture-overview.md)** - 🎯 Master architecture document
   - Complete system design with code-aligned documentation map
   - Links to all 40+ architecture documents
   - Organized by actual code structure

2. **[System Requirements Specification](./specifications/system-requirements-specification.md)** - 🎯 Authoritative requirements (v2.0)
   - 62 functional and non-functional requirements
   - Acceptance criteria and traceability matrix
   - Source of truth for WHAT the system must do

3. **[Investigation Phases Framework](./architecture/investigation-phases-and-ooda-integration.md)** - 🎯 Process framework (v2.1)
   - 7-phase investigation lifecycle (0-6 indexed)
   - OODA tactical steps integration
   - Engagement modes and investigation strategies

4. **[Evidence Collection Design](./architecture/evidence-collection-and-tracking-design.md)** - 🎯 Evidence data models (v2.1)
   - 5-dimensional evidence classification
   - ProblemConfirmation and AnomalyFrame schemas
   - Investigation strategies (Active/Post-Mortem)

5. **[Case and Session Concepts](./architecture/case-and-session-concepts.md)** - 🎯 Authoritative specification (v2.0)
   - User, Client, Session, and Case definitions
   - Authentication-only session model
   - Multi-device support and session resumption
   - ✅ 100% spec-compliant implementation (as of 2025-10-24)

---

## For New Contributors

Start here:
1. **[Getting Started Guide](./getting-started/user-guide.md)** - User perspective
2. **[Developer Guide](./architecture/developer-guide.md)** - Development setup
3. **[Contributing Guidelines](./CONTRIBUTING.md)** - How to contribute
4. **[Code of Conduct](./CODE_OF_CONDUCT.md)** - Community standards

---

## For Developers

### Implementation Guides
- [Context Management](./development/CONTEXT_MANAGEMENT.md) - Typed context system
- [Token Estimation](./development/TOKEN_ESTIMATION.md) - Provider-specific tokenizers
- [Environment Variables](./development/ENVIRONMENT_VARIABLES.md) - Configuration setup
- [How to Add LLM Providers](./development/how-to-add-providers.md) - Provider integration

### Architecture Deep-Dive
- [Architecture Overview](./architecture/architecture-overview.md) - Complete system design
- [Authentication Design](./architecture/authentication-design.md) - ✅ **IMPLEMENTED** - Token-based authentication with RBAC
- [Investigation Framework](./architecture/investigation-phases-and-ooda-integration.md) - ✅ **IMPLEMENTED** - 7-phase OODA investigation framework
- [Data Preprocessing Design](./architecture/data-preprocessing-design.md) - ⭐ **AUTHORITATIVE** - Data types, tools, formats, LLM integration
- [Data Submission Design](./architecture/data-submission-design.md) - How file uploads and data ingestion work
- [Dependency Injection](./architecture/dependency-injection-system.md) - DI container and interfaces

---

## For Operators

### Infrastructure
- [Redis Architecture](./infrastructure/redis-architecture-guide.md) - Redis setup and usage
- [Opik Setup](./infrastructure/opik-setup.md) - LLM observability
- [Local LLM Setup](./infrastructure/Local-LLM-Setup.md) - Local model deployment

### Operational Runbooks
- [Kubernetes Issues](./runbooks/kubernetes/) - Pod, node, deployment issues
- [PostgreSQL Issues](./runbooks/postgresql/) - Database troubleshooting
- [Redis Issues](./runbooks/redis/) - Cache troubleshooting
- [Networking Issues](./runbooks/networking/) - Connection, DNS issues

### Security
- [Role-Based Access Control](./security/role-based-access-control.md) - ✅ **IMPLEMENTED** - User roles, permissions, and management
- [Security Implementation Guide](./security/implementation-guide.md) - Security architecture and best practices
- [PII Sanitization](./security/pii-sanitization-configuration.md) - Privacy protection configuration

### Logging
- [Logging Architecture](./logging/architecture.md) - Logging system design
- [Logging Configuration](./logging/configuration.md) - Setup and configuration
- [Logging Policy](./logging/logging-policy.md) - Standards and policies

---

## Documentation Maintenance

- **Primary Owner**: Architecture Team
- **Review Cycle**: Quarterly (or on major design changes)
- **Contribution Process**: See [CONTRIBUTING.md](./CONTRIBUTING.md)
- **Style Guide**: Markdown with Mermaid diagrams

---

## Directory Index

| Directory | Purpose | Update Frequency |
|-----------|---------|------------------|
| `getting-started/` | User onboarding and quickstart | 🔷 LOW |
| `architecture/` | System architecture, design patterns, specifications | 🔥 HIGH |
| `api/` | API contracts and integration guides | 🔥 HIGH |
| `tools/` | Session-level tools (KB, web, logs, MCP) | 🔶 MEDIUM |
| `development/` | Developer environment and configuration | 🔶 MEDIUM |
| `infrastructure/` | Infrastructure setup and services | 🔶 MEDIUM |
| `testing/` | Testing strategies and patterns | 🔶 MEDIUM |
| `how-to/` | Integration guides and procedures | 🔶 MEDIUM |
| `security/` | Security implementation and policies | 🔶 MEDIUM |
| `logging/` | Logging architecture and configuration | 🔷 LOW |
| `frontend/` | Frontend documentation (website + copilot extension) | 🔶 MEDIUM |
| `runbooks/` | Operational troubleshooting runbooks | 🔷 LOW |
| `archive/` | Historical documentation (migrations, obsolete, releases) | 🔷 LOW |

---

**Last Updated**: 2025-10-12  
**Documentation Version**: 2.1
