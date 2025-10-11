# FaultMaven
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Docker Build](https://img.shields.io/badge/Docker-Ready-blue)](https://docs.docker.com/)
[![FastAPI](https://img.shields.io/badge/Framework-FastAPI-green)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/Tests-341%20passing-brightgreen)](https://github.com/FaultMaven/faultmaven)
[![Coverage](https://img.shields.io/badge/Coverage-71%25-brightgreen)](https://github.com/FaultMaven/faultmaven)
[![Architecture](https://img.shields.io/badge/Architecture-Clean%20DI-blue)](https://github.com/FaultMaven/faultmaven)
[![Providers](https://img.shields.io/badge/LLM%20Providers-7-green)](https://github.com/FaultMaven/faultmaven)

**AI-Powered Troubleshooting Copilot for SRE and DevOps Teams**

*Built with Clean Architecture, Interface-Based Design, and Comprehensive Dependency Injection*

## 🚀 Introduction

FaultMaven is an enterprise-grade open-source AI troubleshooting copilot designed to help SRE and DevOps teams diagnose and resolve complex system issues. Built with clean architecture principles, it serves as the intelligent backend that powers browser-based troubleshooting workflows, combining structured reasoning with centralized knowledge management and multi-LLM provider support.

```mermaid
graph LR
    A[Engineer] --> B(Browser Extension)
    B --> C[FaultMaven API]
    C --> D[AI Reasoning Engine]
    D --> E[Knowledge Base]
    D --> F[System Tools]
```

## ✨ Core Components

### 1. FaultMaven API Server (This Repository)
- **Agentic Framework**: ✅ **IMPLEMENTED** - 7-component agentic system with Plan→Execute→Observe→Re-plan cycles
- **Clean Architecture**: Interface-based design with dependency injection container
- **Multi-LLM Support**: 7 providers (Fireworks, OpenAI, Anthropic, Gemini, HuggingFace, OpenRouter, Local)
- **AI Reasoning Engine**: Advanced agentic workflows with autonomous decision-making capabilities
- **Intelligent Memory System**: Hierarchical memory management with context consolidation and strategic planning
- **Knowledge Management**: RAG-powered document processing with vector store abstraction
- **Data Processing**: Interface-based log/metrics analysis pipelines
- **Privacy-First Security**: Comprehensive PII redaction with Presidio microservice integration
- **User Management**: Authentication, account management, billing, and usage tracking

### 2. [FaultMaven Website](https://faultmaven.com) (This Repository)
- **User Authentication**: Registration, login, password management
- **Account Dashboard**: Profile management, subscription, billing
- **Marketing Pages**: Landing pages, features, pricing, company information
- **Extension Download**: Browser extension distribution and setup guides

### 3. [FaultMaven Copilot](https://github.com/FaultMaven/faultmaven-copilot) (Separate Repository)
- **Browser Extension UI**: Troubleshooting interface with 7 response types
- **Real-time Communication**: Interactive chat with AI agent
- **Evidence Submission**: File uploads and data input
- **Session Management**: Context-aware troubleshooting sessions

## 🧠 Key Features

| Feature | Description | Technology |
|---------|-------------|------------|
| **Clean Architecture** | Interface-based design with dependency injection | `DIContainer`, Interface contracts |
| **Multi-LLM Support** | 7 providers with automatic fallback | Fireworks, OpenAI, Anthropic, Gemini+ |
| **Agentic Framework ✅** | **IMPLEMENTED** - 7-component autonomous AI system with Plan-Execute-Observe-Adapt cycles | 7,770 lines, 55 classes, Production Ready |
| **Knowledge Base (RAG)** | Centralized repository for runbooks and docs | ChromaDB, BGE-M3 |
| **Privacy-First Design** | Comprehensive PII redaction before processing | Presidio microservice, Custom regex |
| **Client Protection System** | Two-phase protection against malicious clients | Rate limiting, ML anomaly detection, Reputation |
| **Interface-Based Tools** | Pluggable tool system with `BaseTool` interface | `KnowledgeBaseTool`, `WebSearchTool` |
| **Context-Aware Analysis** | Intelligent evidence processing with interfaces | `IDataClassifier`, `ILogProcessor` |
| **Multi-Session Management** | Client-based session resumption with concurrent sessions | Multiple sessions per user, device continuity |
| **Comprehensive Observability** | LLM tracing and performance monitoring | Opik, `ITracer` interface |
| **Dual Frontend Architecture** | Website for auth/management, Extension for troubleshooting | Website + Copilot Extension |
| **User Management** | Authentication, billing, usage tracking | User accounts, subscriptions, billing |

## 🚀 Quick Start

### Prerequisites
- [Docker](https://www.docker.com/products/docker-desktop/)
- [Python 3.11+](https://www.python.org/downloads/)
- Clone the repositories:
  ```bash
  # Backend API server + Website frontend
  git clone https://github.com/FaultMaven/faultmaven.git
  
  # Browser extension (separate repository)
  git clone https://github.com/FaultMaven/faultmaven-copilot.git
  ```

### Local Setup with Docker
1. Configure environment variables:
   ```bash
   cp .env.example .env
   # Add your API keys to .env
   ```

2. Start the stack:
   ```bash
   docker-compose up --build -d
   ```

3. Verify services:
   ```bash
   docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
   ```
   ```
   NAMES               STATUS              PORTS
   faultmaven-backend  Up 5 minutes        0.0.0.0:8000->8000/tcp
   chromadb            Up 5 minutes        8000/tcp
   redis               Up 5 minutes        6379/tcp
   ```

4. Start the website frontend:
   ```bash
   # In faultmaven directory (website frontend)
   cd frontend
   npm install
   npm run dev
   # Website will be available at http://localhost:3000
   ```

5. Start the copilot extension development:
   ```bash
   # In faultmaven-copilot directory
   echo "VITE_API_BASE_URL=http://localhost:8000" > .env.local
   npm install
   npm run dev
   # Extension development server will be available
   ```

### Local Development Setup

For local development without Docker:

1. Setup Python environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Configure environment:
   ```bash
   cp .env.example .env
   # Add your API keys to .env
   ```

3. Start FaultMaven:
   ```bash
   # Standard startup
   ./run_faultmaven.sh

   # Or development mode (full transparency, all logs)
   ./run_faultmaven_dev.sh
   ```

4. **Local LLM Service Management** (Optional - for local LLM provider):
   ```bash
   # Start local LLM Docker container service
   ./scripts/local_llm_service.sh start <model_name>

   # Check service status and model consistency
   ./scripts/local_llm_service.sh status

   # Auto-fix model mismatches (stops wrong model, starts correct one)
   ./scripts/local_llm_service.sh check

   # Stop local LLM service
   ./scripts/local_llm_service.sh stop
   ```

## 🧪 Testing

Run the test suite with pytest:

```bash
# Unit tests
pytest -m "unit"

# Integration tests (requires Docker)
pytest -m "integration"

# Full test suite
pytest --cov=faultmaven tests/
```

## 🏗️ Architecture Overview

FaultMaven implements a **modern 7-component Agentic Framework** ✅ **IMPLEMENTED** with true Plan→Execute→Observe→Re-plan autonomous behavior. Built on **Clean Architecture** principles with interface-based programming, comprehensive dependency injection, and clear separation of concerns. The system features intelligent memory management, strategic planning capabilities, and sophisticated error handling for production-ready autonomous AI operations.

### Clean Architecture Layers

```
┌─────────────────────────────────────────────────────────────┐
│                        API Layer                             │
│  (FastAPI Routers, Dependencies, Request/Response Models)    │
├─────────────────────────────────────────────────────────────┤
│                      Service Layer                           │
│  (Business Logic, Orchestration, Interface Dependencies)     │
├─────────────────────────────────────────────────────────────┤
│                 Agentic Framework ✅ ACTIVE                   │
│  (7-Component System: Planning, Memory, Workflows, Safety)   │
├─────────────────────────────────────────────────────────────┤
│                    Core Domain                               │
│  (Agent, Data Processing, Knowledge Base)                    │
├─────────────────────────────────────────────────────────────┤
│                   Infrastructure Layer                       │
│  (LLM Router, Redis, ChromaDB, Security, Observability)      │
├─────────────────────────────────────────────────────────────┤
│                   Interface Contracts                        │
│  (ILLMProvider, ISanitizer, ITracer, BaseTool, etc.)        │
└─────────────────────────────────────────────────────────────┘
```

### Dependency Injection Architecture

**DIContainer** provides centralized dependency management:
- **Singleton Pattern**: Single container instance across application
- **Lazy Initialization**: Components created only when needed  
- **Interface Resolution**: Automatic mapping from interfaces to implementations
- **Health Monitoring**: Built-in health checking for all dependencies
- **Graceful Degradation**: Mock implementations when dependencies unavailable

### Key Components

#### 1. **API Layer** (`api/v1/`)
- RESTful endpoints with versioning support
- Request validation and response formatting
- Dependency injection for services
- Middleware for authentication and rate limiting

#### 2. **Service Layer** (`services/`) - Interface-Based Orchestration
- **AgentService**: Orchestrates troubleshooting workflows using `ILLMProvider`, `ISanitizer`, `ITracer`, `List[BaseTool]`
- **DataService**: Manages data processing using `IDataClassifier`, `ILogProcessor`, `IStorageBackend`
- **KnowledgeService**: Handles knowledge base operations using `IVectorStore`, `IKnowledgeIngester`
- **SessionService**: Provides multi-session per user lifecycle management with client-based resumption using `ISessionStore`

#### 3. **Core Domain** (`core/`)
- **Agent**: LangGraph-based troubleshooting engine with 5-phase doctrine
- **Processing**: Log analysis and data classification
- **Knowledge**: Document ingestion and RAG operations

#### 4. **Infrastructure** (`infrastructure/`) - Interface Implementations
- **LLM**: Multi-provider routing with fallback implementing `ILLMProvider` (7 providers supported)
- **Persistence**: Redis sessions and ChromaDB vectors implementing `ISessionStore`, `IVectorStore`
- **Security**: Comprehensive PII redaction implementing `ISanitizer` with Presidio integration
- **Observability**: Opik tracing implementing `ITracer` with distributed monitoring

#### 5. **Interface Contracts** (`models/interfaces.py`)
Defines abstract contracts for all major components:
- **Infrastructure**: `ILLMProvider`, `ISanitizer`, `ITracer`, `IVectorStore`, `ISessionStore`
- **Processing**: `IDataClassifier`, `ILogProcessor`, `IKnowledgeIngester`
- **Tools**: `BaseTool` with standardized `execute()` and `get_schema()` methods

### Data Flow Example

```mermaid
graph TD
    A[Browser Extension] --> B[API Router]
    B --> C[DI Container]
    C --> D[Service Layer]
    D --> E[Interface Resolution]
    E --> F[Core Domain]
    F --> G[Infrastructure]
    
    subgraph "Interface Layer"
        ILLMProvider
        ISanitizer  
        ITracer
        BaseTool
    end
    
    E --> ILLMProvider
    E --> ISanitizer
    E --> ITracer
    E --> BaseTool
    
    ILLMProvider --> H[7 LLM Providers]
    ISanitizer --> I[Presidio + Fallback]
    ITracer --> J[Opik Tracing]
    BaseTool --> K[KB Tool + Web Search]
```

For detailed architecture documentation, see:
- [Architecture Decision Guide](docs/ARCHITECTURE_DECISION_GUIDE.md) - Comprehensive architectural blueprint and migration strategy
- [System Architecture](docs/architecture/SYSTEM_ARCHITECTURE.md) - Complete interface-based architecture
- [Dependency Injection System](docs/architecture/dependency-injection-system.md) - DI container deep dive
- [Service Patterns](docs/architecture/service-patterns.md) - Interface-based service design

## 🛠️ Development

### Environment Setup
1. Create virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   pip install -r requirements-test.txt
   python -m spacy download en_core_web_lg
   ```

3. Configure pre-commit hooks:
   ```bash
   pre-commit install
   ```

### Clean Architecture Code Structure
```
faultmaven/
├── api/v1/              # API Layer - HTTP endpoints and routing
│   ├── routes/          # Domain-specific routers (agent, data, knowledge, session)
│   └── dependencies.py  # FastAPI dependency injection integration
├── services/            # Service Layer - Business logic orchestration 
│   ├── agent.py    # Troubleshooting workflow orchestration
│   ├── data.py     # Data processing pipeline management
│   ├── knowledge.py # Knowledge base operations
│   └── session.py   # Session lifecycle management
├── core/                # Core Domain - Business logic and entities
│   ├── agent/           # AI reasoning engine (LangGraph + 5-phase doctrine)
│   ├── knowledge/       # RAG document processing
│   └── processing/      # Data classification and log analysis
├── infrastructure/      # Infrastructure - External service integrations
│   ├── llm/            # Multi-provider LLM routing (7 providers)
│   ├── security/       # PII redaction with Presidio integration
│   ├── persistence/    # Redis sessions + ChromaDB vectors
│   └── observability/  # Opik tracing and monitoring
├── tools/              # Agent Tools - Standardized capabilities
│   ├── knowledge_base.py  # RAG tool implementing BaseTool
│   └── web_search.py      # Search tool implementing BaseTool
├── models/             # Data Models and Interface Contracts
│   ├── interfaces.py   # Abstract interfaces for all layers
│   └── *.py           # Request/response DTOs and domain entities
├── container.py        # Dependency Injection Container (DIContainer)
├── config/             # Configuration and Feature Flags
└── main.py             # FastAPI application with DI integration
```

## ⚙️ Configuration

### LLM Provider Setup

FaultMaven supports **7 LLM providers** with automatic fallback chains managed through a centralized provider registry:

```env
# Primary provider (7 options available)
CHAT_PROVIDER="fireworks"  # fireworks, openai, anthropic, gemini, huggingface, openrouter, local

# Provider API keys (providers auto-initialize based on available keys)
FIREWORKS_API_KEY="fw_your_api_key"           # Fireworks AI (recommended)
OPENAI_API_KEY="sk_your_openai_key"          # OpenAI GPT models
ANTHROPIC_API_KEY="sk-ant-your_key"          # Claude 3.5 Sonnet
GEMINI_API_KEY="your_google_ai_key"          # Google Gemini
HUGGINGFACE_API_KEY="hf_your_token"          # HuggingFace models
OPENROUTER_API_KEY="sk-or-your_key"          # OpenRouter multi-provider
LOCAL_LLM_URL="http://localhost:11434"       # Local/Ollama (no API key needed)
```

**Automatic Fallback Chain**: Primary → Fireworks → OpenAI → Local (based on available API keys)

**Supported Models by Provider**:
- **Fireworks**: `llama-v3p1-8b-instruct`, `llama-v3p1-70b-instruct`, `mixtral-8x7b-instruct`
- **OpenAI**: `gpt-4o`, `gpt-4o-mini`, `gpt-3.5-turbo`
- **Anthropic**: `claude-3-5-sonnet-20241022`, `claude-3-haiku-20240307`, `claude-3-opus-20240229`
- **Gemini**: `gemini-1.5-pro`, `gemini-1.5-flash`, `gemini-pro-vision`
- **HuggingFace**: `tiiuae/falcon-7b-instruct`, `microsoft/DialoGPT-large`
- **OpenRouter**: Access to multiple providers through unified API
- **Local**: Ollama, vLLM, any OpenAI-compatible server

For detailed configuration and adding new providers, see: [How to Add Providers](docs/how-to-add-providers.md)

### Environment Variables

Copy `.env.example` to `.env` and configure:
- **Required**: At least one LLM provider API key
- **Optional**: Observability, web search, local model servers
- **Timeout Configuration**: `LLM_REQUEST_TIMEOUT=30` (applies to all providers)
- **Local LLM Setup**: `LOCAL_LLM_URL` and `LOCAL_LLM_MODEL` for containerized local models

### Observability & Monitoring

**Built-in Health Monitoring**:
```bash
# Check dependency injection container health
curl http://localhost:8000/health/dependencies

# Response includes status of all injected dependencies:
{
  "status": "healthy",  # healthy | degraded | not_initialized
  "components": {
    "llm_provider": true,
    "sanitizer": true, 
    "tracer": true,
    "tools_count": 2,
    "agent_service": true,
    "data_service": true,
    "knowledge_service": true
  }
}
```

**Opik LLM Observability**:

For **team Opik server** (default configuration):
```bash
# Ensure hostname resolves
echo "192.168.0.111 opik.faultmaven.local" >> /etc/hosts
./run_faultmaven.sh
```

For **custom Opik instances**:
```bash
# Configure custom Opik server
cp scripts/config/opik_remote.sh.example scripts/config/opik_custom.sh
# Edit with your Opik server details
source scripts/config/opik_custom.sh
./run_faultmaven.sh
```

**Accessing Monitoring**:
- **Opik Dashboard**: `http://opik.faultmaven.local:30080` - LLM traces and performance
- **Health Endpoint**: `http://localhost:8000/health/dependencies` - Container health
- **API Documentation**: `http://localhost:8000/docs` - Interactive API explorer

## 📜 License

Apache 2.0 - See [LICENSE](LICENSE) for details.

## 🤝 Contributing

We welcome contributions! Please see our [Contribution Guidelines](docs/CONTRIBUTING.md) for details.

**Architecture Guidelines**: When contributing, please follow the interface-based design patterns. All new components should:
1. Define and implement appropriate interfaces from `models/interfaces.py`
2. Integrate with the dependency injection container
3. Include comprehensive unit tests with interface mocks
4. Follow the clean architecture layer separation

## 📬 Contact

For inquiries: [support@faultmaven.ai](mailto:support@faultmaven.ai)  
Join our [Discord Community](https://discord.com/faultmaven) for real-time discussion.

## 📚 Documentation

### **Documentation Structure**

FaultMaven documentation is organized into strategic and tactical levels:

📚 **Complete documentation is in [`docs/`](./docs/)**

### Quick Links

- 🚀 **[Getting Started](./docs/getting-started/)** - User guide and quickstart
- 🏗️ **[Architecture Overview](./docs/architecture/architecture-overview.md)** - 🎯 Master architecture document
- 📋 **[System Requirements](./docs/specifications/system-requirements-specification.md)** - 🎯 Requirements (v2.0)
- 🔌 **[API Documentation](./docs/api/)** - OpenAPI spec and integration guides
- 💻 **[Development](./docs/development/)** - Setup, guides, best practices
- 🧪 **[Testing](./docs/testing/)** - Testing strategies and patterns
- 🔒 **[Security](./docs/security/)** - Security and protection systems
- 🏗️ **[Infrastructure](./docs/infrastructure/)** - Infrastructure setup and configuration

### Documentation Structure

See **[docs/README.md](./docs/README.md)** for complete documentation index.

**Master Documents** (Authoritative Sources):
- 🎯 [Architecture Overview](./docs/architecture/architecture-overview.md) - System architecture (v2.0, code-aligned)
- 🎯 [System Requirements](./docs/specifications/system-requirements-specification.md) - Requirements (v2.0, 62 requirements)
- 🎯 [Investigation Phases Framework](./docs/architecture/investigation-phases-and-ooda-integration.md) - Process framework (v2.1, 7 phases)
- 🎯 [Evidence Collection Design](./docs/architecture/evidence-collection-and-tracking-design.md) - Evidence models (v2.1)

### Documentation by Role

#### For Product Managers & Stakeholders
- **[System Requirements Specification](./docs/specifications/system-requirements-specification.md)** - 62 requirements: what the system must do
- **[Case and Session Concepts](./docs/specifications/CASE_SESSION_CONCEPTS.md)** - Core concepts and distinctions

#### For Architects & Technical Leads
- **[Architecture Overview](./docs/architecture/architecture-overview.md)** - Complete system design (code-aligned, 40+ docs)
- **[Investigation Phases Framework](./docs/architecture/investigation-phases-and-ooda-integration.md)** - 7-phase process (v2.1)
- **[Evidence Collection Design](./docs/architecture/evidence-collection-and-tracking-design.md)** - Evidence models (v2.1)
- **[Agentic Framework](./docs/architecture/agentic-framework-design-specification.md)** - 7-component AI framework
- **[Architecture Diagrams](./docs/architecture/diagrams/)** - Visual system representations

#### For Backend Developers
- **[Developer Guide](./docs/architecture/developer-guide.md)** - Development workflow and best practices
- **[Dependency Injection System](./docs/architecture/dependency-injection-system.md)** - DI container and patterns
- **[Service Patterns](./docs/architecture/service-patterns.md)** - Service layer implementation
- **[Context Management](./docs/development/CONTEXT_MANAGEMENT.md)** - Typed QueryContext usage
- **[Token Estimation](./docs/development/TOKEN_ESTIMATION.md)** - Provider-specific tokenizers
- **[How to Add Providers](./docs/development/how-to-add-providers.md)** - LLM provider integration

#### For Frontend Developers
- **[Website Frontend Guide](./docs/frontend/website-guide.md)** - Landing pages, auth, user management
- **[Website Components](./docs/frontend/website-components.md)** - Component library
- **[API Integration](./docs/frontend/api-integration.md)** - Frontend-backend integration
- **[Copilot Extension](https://github.com/FaultMaven/faultmaven-copilot)** - Browser extension (separate repo)

#### For DevOps & Operations
- **[Infrastructure Guides](./docs/infrastructure/)** - Redis, ChromaDB, Opik, local LLM
- **[Logging](./docs/logging/)** - Architecture, configuration, operations
- **[Runbooks](./docs/runbooks/)** - Kubernetes, PostgreSQL, Redis, networking
- **[Deployment Guide](./docs/architecture/DEPLOYMENT_GUIDE.md)** - Production deployment

#### For QA & Testers
- **[Testing Guide](./docs/architecture/testing-guide.md)** - Testing strategies
- **[Architecture Testing](./docs/testing/architecture-testing-guide.md)** - Architecture compliance testing
- **[Test Patterns](./docs/testing/new-test-patterns.md)** - Modern testing patterns

### **Key Features Documentation**

#### **Response Type System**
- **7 Response Types**: ANSWER, PLAN_PROPOSAL, CLARIFICATION_REQUEST, CONFIRMATION_REQUEST, SOLUTION_READY, NEEDS_MORE_DATA, ESCALATION_REQUIRED
- **Copilot Extension Components**: Each response type has dedicated React components with specific behaviors in the browser extension
- **Website Frontend**: Landing pages, authentication, and user management (separate from the 7 response types)
- **Implementation**: 
  - **Copilot Extension**: See [Copilot Repository](https://github.com/FaultMaven/faultmaven-copilot) for complete implementation
  - **Website**: See [Website Component Library](../docs/frontend/website-components.md)

#### **Intelligent Communication**
- **Memory Management**: Hierarchical memory system with context awareness
- **Advanced Prompting**: Dynamic prompt assembly with context injection
- **Strategic Planning**: Multi-phase planning and problem decomposition
- **Implementation**: See [System Requirements - Advanced Communication Section](../docs/FAULTMAVEN_SYSTEM_REQUIREMENTS.md#advanced-communication-layer-memory-prompting--planning)

#### **Case Lifecycle Management**
- **Status Management**: Comprehensive case status tracking
- **Termination Logic**: Intelligent case closure and escalation
- **Implementation**: See [System Requirements - Case Lifecycle Section](../docs/FAULTMAVEN_SYSTEM_REQUIREMENTS.md#case-lifecycle-management)

---

**Technical Documentation**:
- [Architecture Deep Dive](docs/architecture/SYSTEM_ARCHITECTURE.md)
- [DI Container Guide](docs/architecture/dependency-injection-system.md)
- [Container Usage Guide](docs/architecture/container-usage-guide.md)
- [LLM Provider Setup](docs/how-to-add-providers.md)
- [Testing Guide](tests/README.md)
- [Testing Architecture Guide](docs/architecture/testing-guide.md)
- [Migration Guide](docs/migration/import-migration-guide.md)