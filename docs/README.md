# FaultMaven Documentation

This directory contains comprehensive documentation for the FaultMaven AI-powered troubleshooting system. The documentation is organized by functional area to help developers, operators, and users understand and work with the system.

## Quick Navigation

### 🏗️ Architecture & Design
- **[System Architecture](architecture/SYSTEM_ARCHITECTURE.md)** - Complete system overview with visual diagrams
- **[Agentic Framework Design](architecture/agentic-framework-design-specification.md)** - Comprehensive 7-component agentic framework specification
- **[Agentic Framework Architecture](architecture/AGENTIC_FRAMEWORK_ARCHITECTURE.md)** - Implementation overview and component details
- **[Component Interactions](architecture/COMPONENT_INTERACTIONS.md)** - Detailed interaction patterns and data flows
- **[Case-Agent Integration Design](architecture/CASE_AGENT_INTEGRATION_DESIGN.md)** - Technical design for case-agent integration
- **[Configuration System Refactor](architecture/CONFIGURATION_SYSTEM_REFACTOR_DESIGN.md)** - Unified configuration system design
- **[Interface-Based Design](architecture/interface-based-design.md)** - Clean architecture patterns
- **[Dependency Injection System](architecture/dependency-injection-system.md)** - DI container usage
- **[Service Patterns](architecture/service-patterns.md)** - Service layer design patterns
- **[Infrastructure Layer Guide](architecture/infrastructure-layer-guide.md)** - Internal vs external service patterns
- **[Deployment Guide](architecture/DEPLOYMENT_GUIDE.md)** - Production deployment instructions

### 📊 Logging & Observability  
- **Logging System**: ✅ **100% COMPLETE** - Production-ready logging with zero duplicates and comprehensive observability
- **[Logging Architecture](logging/architecture.md)** - Technical architecture documentation
- **[Implementation Guide](logging/implementation-guide.md)** - Complete implementation overview  
- **[Configuration Reference](logging/configuration.md)** - Environment variables and setup
- **[Operations Runbook](logging/operations-runbook.md)** - Production operations guide
- **[Testing Guide](logging/testing-guide.md)** - Testing patterns and examples

### 🔧 Infrastructure Documentation
- **[Redis Architecture Guide](infrastructure/redis-architecture-guide.md)** - 🆕 **NEW** - Redis logging optimization and patterns

### 🔌 API Documentation
- **[API Reference](api/README.md)** - Complete API documentation with examples
- **[API Contract Matrix](api/API_CONTRACT_MATRIX.md)** - Single-page API contract reference
- **[OpenAPI Specification](api/openapi.yaml)** - Authoritative API specification
- **[Troubleshooting Guide](api/v3.1.0-TROUBLESHOOTING-GUIDE.md)** - API troubleshooting and debugging
- **[Interactive API Docs](http://localhost:8000/docs)** - Available at `/docs` when server is running

### 🧪 Testing & Quality
- **[Testing Architecture](testing/REBUILT_TESTING_STANDARDS.md)** - Testing standards and patterns
- **[Test Structure](../tests/README.md)** - Test organization and execution
- **[Performance Testing](../tests/performance/)** - Load testing and benchmarks

### 🔧 Development Guides
- **[How to Add Providers](how-to-add-providers.md)** - Adding new LLM providers
- **[Developer Guide](architecture/developer-guide.md)** - Development workflow and patterns
- **[Container Usage Guide](architecture/container-usage-guide.md)** - Dependency injection patterns

### 📋 Specifications
- **[Interface Documentation](specifications/INTERFACE_DOCUMENTATION_SPEC.md)** - Interface contracts and compliance
- **[Configuration Management](specifications/CONFIGURATION_MANAGEMENT_SPEC.md)** - Configuration patterns
- **[Session Management](specifications/SESSION_MANAGEMENT_SPEC.md)** - Session lifecycle specification
- **[Error Context Enhancement](specifications/ERROR_CONTEXT_ENHANCEMENT_SPEC.md)** - Error handling patterns

### 🚀 Operations & Deployment
- **[Opik Setup Guide](opik-setup.md)** - LLM observability setup
- **[Migration Guides](migration/)** - System migration documentation
- **[Troubleshooting](troubleshooting/)** - Common issues and solutions

### 📈 Performance & Monitoring
- **[Testing Guide](architecture/testing-guide.md)** - Architecture testing patterns

## Project Status Overview

### ✅ Completed Systems (Production Ready)
- **Logging Infrastructure**: 100% complete with zero duplicate logs
- **Clean Architecture**: Interface-based design with DI container
- **Multi-LLM Support**: 7 providers with automatic fallback
- **Session Management**: Redis-backed session lifecycle
- **Security Layer**: PII redaction with Presidio integration
- **Testing Framework**: 71% coverage with 341+ passing tests

### 🏗️ Core Architecture
- **Clean Architecture**: Interface-based design with dependency injection
- **Service-Oriented**: Clear separation between API, Service, Core, and Infrastructure layers
- **Multi-LLM Support**: 7 providers (Fireworks, OpenAI, Anthropic, Gemini, HuggingFace, OpenRouter, Local)
- **Privacy-First**: Comprehensive PII redaction before LLM processing
- **RAG Knowledge Base**: ChromaDB with BGE-M3 embeddings
- **5-Phase SRE Doctrine**: Structured troubleshooting methodology

### 📊 Key Metrics
- **Test Coverage**: 71% (341+ passing tests)
- **Logging Overhead**: < 0.5% performance impact
- **Duplicate Prevention**: 100% effectiveness
- **LLM Providers**: 7 providers with automatic failover
- **API Endpoints**: Complete OpenAPI documentation
- **Documentation**: 126+ markdown files (now organized and cleaned)

## Getting Started

### For Developers
1. Review **[System Architecture](architecture/SYSTEM_ARCHITECTURE.md)** for complete system overview
2. Read **[Agentic Framework Design](architecture/agentic-framework-design-specification.md)** for detailed agentic architecture
3. Check **[Developer Guide](architecture/developer-guide.md)** for development workflow
4. Study **[Container Usage Guide](architecture/container-usage-guide.md)** for dependency injection patterns
5. Configuration: **[FLAGS_AND_CONFIG.md](FLAGS_AND_CONFIG.md)**, **[LOGGING_POLICY.md](LOGGING_POLICY.md)**

#### Run the API locally
```bash
uvicorn faultmaven.faultmaven.main:app --reload
```

#### Enable debug and core features (optional)
```bash
export FAULTMAVEN_DEBUG=1
export FAULTMAVEN_GATEWAY=1 FAULTMAVEN_ROUTER=1 FAULTMAVEN_CONFIDENCE=1 FAULTMAVEN_LOOP_GUARD=1
```

#### Run tests
```bash
pytest -q
```

### For Operations Teams
1. Review **[Deployment Guide](architecture/DEPLOYMENT_GUIDE.md)** for production setup
2. Study **[Logging Operations Runbook](logging/operations-runbook.md)** for monitoring
3. Configure using **[Configuration Reference](logging/configuration.md)**
4. Set up observability with **[Opik Setup Guide](opik-setup.md)**

### For API Users
1. Visit **[API Documentation](api/README.md)** for complete reference
2. Use interactive docs at `/docs` when server is running
3. Review **[OpenAPI Specification](api/openapi.json)** for integration

### Manual Test Checklist (No‑LLM Phase)

Use these to validate expected deterministic behaviors before LLM wiring:

- Greetings
  - Input: `hello`
  - Expect: 200, friendly onboarding message, no clarifier.

- Definition/General
  - Input: `what is dns`, `what is llm`
  - Expect: 200, concise definition, no clarifier.

- Performance
  - Input: `why my server is slow`
  - Expect: 200, actionable performance checklist, no clarifier.

- Best Practices
  - Input: `What’s the rollback procedure for a bad deploy?`
  - Expect: 200, high‑level rollback steps.
  - Input: `How often should we run disaster recovery drills?`
  - Expect: 200, cadence & scope checklist, no placeholders like `<DATE_TIME>`.
  - Input: `What’s the safest way to drain traffic from a node?`
  - Expect: 200 safe draining sequence, or `CONFIRMATION_REQUEST` via PolicyEngine. Never 500.
  - Input: `What’s the best backup strategy for a high‑write database?`
  - Expect: 200, strategy bullets (PITR/WAL, I/O throttling, encryption, retention).

- Safety‑Sensitive
  - Input: `How do we safely delete production data?`
  - Expect: 200 with `response_type=CONFIRMATION_REQUEST`, content includes confirmation text and risks; never 500.

- Error Semantics
  - If LLM disabled (current phase): all above paths must return deterministic content; no external calls; no 500.

Notes
- If any of the above returns the Clarifier prompt, restart the API to ensure the updated Gateway and AgentService are loaded.
- See `docs/ARCHITECTURE_BLUEPRINT_MODULAR_MONOLITH.md` for the interim no‑LLM behavioral contract.

### For Contributors
1. Read **[Testing Standards](testing/REBUILT_TESTING_STANDARDS.md)** for quality guidelines
2. Follow **[Module Guidelines](architecture/module-guidelines.md)** for code organization
3. Review **[Interface Documentation](specifications/INTERFACE_DOCUMENTATION_SPEC.md)** for contracts

## Documentation Standards

All FaultMaven documentation follows these standards:
- **Comprehensive**: Complete coverage of functionality
- **Current**: Updated with every system change
- **Accessible**: Clear language for all skill levels
- **Visual**: Mermaid diagrams for complex concepts
- **Executable**: Code examples that actually work
- **Cross-Referenced**: Proper linking between related documents

## Recent Updates

### 2025-01-21: Major Documentation Cleanup & Consolidation
- ✅ **CONSOLIDATED** architecture blueprints into single [Architecture Decision Guide](../ARCHITECTURE_DECISION_GUIDE.md)
- ✅ **MOVED** completed implementation docs (logging, architecture phases) to `recycle/completed-implementations/`
- ✅ **ORGANIZED** documentation into clear categories: active docs vs. historical references
- ✅ **SIMPLIFIED** navigation by removing redundant and outdated references
- ✅ **UPDATED** main README and docs index to reflect current architecture decisions
- ✅ **CLEANED** up 15+ redundant documentation files while preserving essential content

### Recent Architecture Improvements
- **Complete Logging Strategy**: 100% implementation with zero duplicate logs
- **Enhanced Context Management**: Session and user context propagation
- **Legacy Cleanup**: Removed 312 lines of obsolete logging code
- **Performance Optimization**: Achieved < 0.5% logging overhead
- **Health Monitoring**: Dedicated `/health/logging` endpoint

## Navigation Tips

- Use the **Quick Navigation** section above for direct links to key documentation
- Each section includes both high-level overviews and detailed technical documentation  
- Visual diagrams are included in architecture documents for complex concepts
- All code examples are tested and functional
- Cross-references between documents provide comprehensive coverage

For questions or improvements to documentation, please refer to the contribution guidelines in the main repository.