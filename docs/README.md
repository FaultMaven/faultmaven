# FaultMaven Documentation

This directory contains comprehensive documentation for the FaultMaven AI-powered troubleshooting system. The documentation is organized by functional area to help developers, operators, and users understand and work with the system.

## Quick Navigation

### 🏗️ Architecture & Design
- **[System Architecture](architecture/SYSTEM_ARCHITECTURE.md)** - Complete system overview with visual diagrams
- **[Component Interactions](architecture/COMPONENT_INTERACTIONS.md)** - Detailed interaction patterns and data flows
- **[Deployment Guide](architecture/DEPLOYMENT_GUIDE.md)** - Production deployment instructions
- **[Current Architecture](architecture/current-architecture.md)** - Current implementation status
- **[Interface-Based Design](architecture/interface-based-design.md)** - Clean architecture patterns
- **[Dependency Injection System](architecture/dependency-injection-system.md)** - DI container usage

### 📊 Logging & Observability
- **[Logging Implementation Status](logging/LOGGING_IMPLEMENTATION_STATUS.md)** - ✅ **COMPLETE** - Current logging system status
- **[Logging Architecture](logging/architecture.md)** - Technical architecture documentation
- **[Implementation Guide](logging/implementation-guide.md)** - Complete implementation overview  
- **[Configuration Reference](logging/configuration.md)** - Environment variables and setup
- **[Operations Runbook](logging/operations-runbook.md)** - Production operations guide
- **[Testing Guide](logging/testing-guide.md)** - Testing patterns and examples

### 🔌 API Documentation
- **[API Reference](api/README.md)** - Complete API documentation with examples
- **[OpenAPI Specification](api/openapi.json)** - Machine-readable API spec
- **[Interactive API Docs](../docs)** - Available at `/docs` when server is running

### 🧪 Testing & Quality
- **[Testing Architecture](testing/REBUILT_TESTING_STANDARDS.md)** - Testing standards and patterns
- **[Test Structure](../tests/README.md)** - Test organization and execution
- **[Performance Testing](../tests/performance/)** - Load testing and benchmarks

### 🔧 Development Guides
- **[How to Add Providers](how-to-add-providers.md)** - Adding new LLM providers
- **[Developer Guide](architecture/developer-guide.md)** - Development workflow and patterns
- **[Container Usage Guide](architecture/container-usage-guide.md)** - Dependency injection patterns
- **[Module Guidelines](architecture/module-guidelines.md)** - Code organization standards

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
- **[Performance Tuning Guide](FaultMaven_Performance_Tuning_Guide.md)** - System optimization
- **[Context Management Analysis](Context_Management_Analysis.md)** - Context system analysis

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
1. Start with **[System Architecture](architecture/SYSTEM_ARCHITECTURE.md)** for system overview
2. Review **[Interface-Based Design](architecture/interface-based-design.md)** for architectural patterns
3. Read **[Developer Guide](architecture/developer-guide.md)** for development workflow
4. Check **[Container Usage Guide](architecture/container-usage-guide.md)** for dependency injection

### For Operations Teams
1. Review **[Deployment Guide](architecture/DEPLOYMENT_GUIDE.md)** for production setup
2. Study **[Logging Operations Runbook](logging/operations-runbook.md)** for monitoring
3. Configure using **[Configuration Reference](logging/configuration.md)**
4. Set up observability with **[Opik Setup Guide](opik-setup.md)**

### For API Users
1. Visit **[API Documentation](api/README.md)** for complete reference
2. Use interactive docs at `/docs` when server is running
3. Review **[OpenAPI Specification](api/openapi.json)** for integration

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

### 2025-01-13: Repository Cleanup & Documentation Refresh
- ✅ Moved obsolete summary files to `recycle/` folder
- ✅ Updated logging documentation to reflect 100% completion status
- ✅ Enhanced CLAUDE.md with latest architecture improvements
- ✅ Organized documentation navigation with this comprehensive index
- ✅ Cleaned up 14 obsolete summary/report files
- ✅ Verified all logging documentation accuracy

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