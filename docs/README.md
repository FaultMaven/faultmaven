# FaultMaven Documentation

Master index for all FaultMaven documentation.

## Quick Navigation

- 🚀 **[Getting Started](./getting-started/)** - Installation, quickstart, user guide
- 🏗️ **[Architecture](./architecture/architecture-overview.md)** - System architecture and design (master document)
- 📋 **[Specifications](./specifications/)** - Requirements and technical specifications
- 🔌 **[API Documentation](./api/)** - API contracts, OpenAPI spec, integration guides
- 💻 **[Development](./development/)** - Developer guides, environment setup, best practices
- 🏗️ **[Infrastructure](./infrastructure/)** - Infrastructure setup, Redis, ChromaDB, LLM providers
- 🧪 **[Testing](./testing/)** - Testing strategies, patterns, and guides
- 📚 **[Guides](./guides/)** - How-to guides and tutorials
- 🔒 **[Security](./security/)** - Security implementation and policies
- 📝 **[Logging](./logging/)** - Logging architecture and configuration
- 🎯 **[Features](./features/)** - Feature documentation and specifications
- 📖 **[Runbooks](./runbooks/)** - Operational runbooks for common issues
- 🔧 **[Troubleshooting](./troubleshooting/)** - Troubleshooting guides
- 🚀 **[Releases](./releases/)** - Release notes and changelog
- 🔄 **[Migration](./migration/)** - Migration guides and procedures

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
- [Agentic Framework](./architecture/agentic-framework-design-specification.md) - 7-component AI framework
- [Dependency Injection](./architecture/dependency-injection-system.md) - DI container and interfaces
- [Query Classification](./architecture/query-classification-and-prompt-engineering.md) - Intent taxonomy and prompts

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
| `getting-started/` | User onboarding | 🔷 LOW |
| `architecture/` | System architecture (40+ docs) | 🔥 HIGH |
| `specifications/` | Requirements and specs | 🔷 LOW |
| `api/` | API documentation | 🔥 HIGH |
| `development/` | Developer guides | 🔶 MEDIUM |
| `infrastructure/` | Infrastructure setup | 🔶 MEDIUM |
| `testing/` | Testing documentation | 🔶 MEDIUM |
| `security/` | Security guides | 🔶 MEDIUM |
| `logging/` | Logging documentation | 🔷 LOW |
| `frontend/` | Frontend documentation | 🔶 MEDIUM |
| `features/` | Feature specs | 🔶 MEDIUM |
| `guides/` | How-to guides | 🔷 LOW |
| `runbooks/` | Operational runbooks | 🔷 LOW |
| `troubleshooting/` | Troubleshooting guides | 🔷 LOW |
| `releases/` | Release notes | 🔷 LOW |
| `migration/` | Migration guides | 🔷 LOW |

---

**Last Updated**: 2025-10-11  
**Documentation Version**: 2.0
